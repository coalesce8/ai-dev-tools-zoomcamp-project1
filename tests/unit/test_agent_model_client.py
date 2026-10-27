import json
import logging

import httpx2 as httpx
import pytest
from runwatch.agent.model import AnthropicModelClient, ModelApiError, ModelResponse

from tests.unit.agent_loop_support import FakeModelClient, no_network  # noqa: F401

KEY = "sk-ant-MARKER-KEY-123"
SYSTEM = [{"type": "text", "text": "SYSTEM-TEXT"}]
TOOLS = [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]
MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "MSG-TEXT"}]}]


def make(handler):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(wrapped))
    return AnthropicModelClient(KEY, 5, http_client=http), seen


def ask(client, model="claude-test-model"):
    return client.create(
        model=model, system=SYSTEM, tools=TOOLS, messages=MESSAGES, max_tokens=4096
    )


def ok_body(**usage):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test-model",
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "tu_9", "name": "get_stats", "input": {}},
        ],
        "usage": {"input_tokens": 11, "output_tokens": 7, **usage},
    }


def test_request_carries_model_system_tools_and_messages():
    client, seen = make(lambda r: httpx.Response(200, json=ok_body()))
    ask(client)
    body = json.loads(seen[0].content)
    assert body["model"] == "claude-test-model"
    assert body["system"] == SYSTEM and body["tools"] == TOOLS
    assert body["messages"] == MESSAGES and body["max_tokens"] == 4096


def test_reply_becomes_a_model_response_of_plain_dicts():
    client, _ = make(
        lambda r: httpx.Response(
            200, json=ok_body(cache_read_input_tokens=5, cache_creation_input_tokens=3)
        )
    )
    response = ask(client)
    assert isinstance(response, ModelResponse)
    assert response.stop_reason == "tool_use"
    assert response.content == [
        {"type": "text", "text": "looking"},
        {"type": "tool_use", "id": "tu_9", "name": "get_stats", "input": {}},
    ]
    assert all(type(b) is dict for b in response.content)
    usage = response.usage
    assert (usage.input_tokens, usage.output_tokens) == (11, 7)
    assert (usage.cache_creation_input_tokens, usage.cache_read_input_tokens) == (3, 5)
    assert usage.total == 11 + 7 + 3 + 5


def test_missing_usage_fields_default_to_zero():
    client, _ = make(lambda r: httpx.Response(200, json=ok_body()))
    usage = ask(client).usage
    assert usage.cache_creation_input_tokens == 0 == usage.cache_read_input_tokens


def raise_timeout(request):
    raise httpx.ReadTimeout("slow", request=request)


def raise_connect(request):
    raise httpx.ConnectError("nope", request=request)


@pytest.mark.parametrize(
    ("handler", "status", "kind"),
    [
        (lambda r: httpx.Response(500, json={"error": "BODY-MARKER"}), 500, None),
        (lambda r: httpx.Response(429, json={"error": "BODY-MARKER"}), 429, None),
        (lambda r: httpx.Response(401, json={"error": "BODY-MARKER"}), 401, None),
        (raise_timeout, None, "APITimeoutError"),
        (raise_connect, None, "APIConnectionError"),
    ],
)
def test_failures_raise_model_api_error_without_body_or_key(handler, status, kind):
    client, seen = make(handler)
    with pytest.raises(ModelApiError) as info:
        ask(client)
    error = info.value
    assert error.status == status
    assert error.kind == (kind or error.kind) and error.kind.endswith("Error")
    text = str(error)
    assert text == f"model API error: kind={error.kind} status={status}"
    for secret in (KEY, "BODY-MARKER", "MSG-TEXT", "SYSTEM-TEXT"):
        assert secret not in text and secret not in repr(error)
    assert error.__cause__ is None and error.__suppress_context__
    assert len(seen) == 1  # the SDK must not retry on its own


def test_key_is_never_in_repr_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    client, _ = make(lambda r: httpx.Response(500, json={}))
    with pytest.raises(ModelApiError):
        ask(client)
    assert KEY not in repr(client)
    assert all(KEY not in r.getMessage() for r in caplog.records)


def test_fake_replays_records_and_fails_when_the_script_runs_out():
    fake = FakeModelClient([ModelResponse("end_turn"), ModelApiError("X", 500)])
    fake.create(model="m", system=[], tools=[], messages=[], max_tokens=1)
    with pytest.raises(ModelApiError):
        fake.create(model="m2", system=[], tools=[], messages=[], max_tokens=1)
    assert [c["model"] for c in fake.calls] == ["m", "m2"]
    with pytest.raises(AssertionError):
        fake.create(model="m3", system=[], tools=[], messages=[], max_tokens=1)
