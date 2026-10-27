"""Error envelope tests. No database: routes come from a test-only router."""

from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from runwatch.api.app import create_app
from runwatch.api.errors import ApiError, ConflictError, NotFoundError
from runwatch.db import get_session
from runwatch.settings import Settings
from sqlalchemy.orm import Session

UNREACHABLE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:1/x"


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    count: int


extra_router = APIRouter(prefix="/test")


@extra_router.post("/items")
def create_item(item: Item) -> dict[str, str]:
    return {"name": item.name}


@extra_router.get("/range")
def in_range(n: Annotated[int, Query(ge=1, le=10)]) -> dict[str, int]:
    return {"n": n}


@extra_router.get("/api-error")
def api_error() -> None:
    raise ApiError(418, "teapot", "I am a teapot")


@extra_router.get("/not-found")
def not_found() -> None:
    raise NotFoundError("Job 42 not found")


@extra_router.get("/conflict")
def conflict() -> None:
    raise ConflictError("Job is not running")


@extra_router.get("/http/{status}")
def http_exception(status: int) -> None:
    raise HTTPException(status_code=status, detail=f"detail {status}")


@extra_router.get("/crash")
def crash() -> None:
    raise RuntimeError("secret-crash-text")


@pytest.fixture
def client() -> TestClient:
    app = create_app(Settings(database_url=UNREACHABLE_URL))
    app.include_router(extra_router)
    return TestClient(app, raise_server_exceptions=False)


def assert_envelope(response, status: int, code: str) -> str:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    return body["error"]["message"]


def test_validation_error_too_short_string(client):
    response = client.post("/test/items", json={"name": "", "count": 1})
    message = assert_envelope(response, 400, "validation_error")
    assert message == "body.name: String should have at least 1 character"


def test_validation_error_bad_type(client):
    response = client.post("/test/items", json={"name": "a", "count": "lots"})
    message = assert_envelope(response, 400, "validation_error")
    assert message.startswith("body.count: ")
    assert "lots" not in message


def test_validation_error_missing_field(client):
    response = client.post("/test/items", json={"name": "a"})
    message = assert_envelope(response, 400, "validation_error")
    assert message == "body.count: Field required"


def test_validation_error_out_of_range_query(client):
    response = client.get("/test/range", params={"n": 99})
    message = assert_envelope(response, 400, "validation_error")
    assert message == "query.n: Input should be less than or equal to 10"
    assert "99" not in message


def test_validation_error_malformed_json(client):
    response = client.post(
        "/test/items",
        content=b'{"name": "a",',
        headers={"content-type": "application/json"},
    )
    message = assert_envelope(response, 400, "validation_error")
    assert message.startswith("body")
    assert "JSON" in message


def test_validation_message_does_not_echo_value(client):
    secret = "x" * 101 + "-submitted-value"
    response = client.post("/test/items", json={"name": secret, "count": 1})
    message = assert_envelope(response, 400, "validation_error")
    assert "submitted-value" not in response.text
    assert message.startswith("body.name: ")


@pytest.mark.parametrize("path", ["/api/v1/nope", "/internal/v1/nope", "/nope"])
def test_unknown_path_is_not_found(client, path):
    assert_envelope(client.get(path), 404, "not_found")


def test_wrong_method_is_method_not_allowed(client):
    response = client.post("/healthz")
    assert_envelope(response, 405, "method_not_allowed")
    assert "GET" in response.headers["allow"]


def test_api_error_keeps_status_code_and_message(client):
    message = assert_envelope(client.get("/test/api-error"), 418, "teapot")
    assert message == "I am a teapot"


def test_not_found_error(client):
    message = assert_envelope(client.get("/test/not-found"), 404, "not_found")
    assert message == "Job 42 not found"


def test_conflict_error(client):
    message = assert_envelope(client.get("/test/conflict"), 409, "conflict")
    assert message == "Job is not running"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "not_found"),
        (405, "method_not_allowed"),
        (409, "conflict"),
        (403, "http_error"),
        (429, "http_error"),
        (502, "http_error"),
    ],
)
def test_http_exception_keeps_status(client, status, code):
    message = assert_envelope(client.get(f"/test/http/{status}"), status, code)
    assert message == f"detail {status}"


def test_unhandled_exception_is_internal_error(client, caplog):
    response = client.get("/test/crash")
    message = assert_envelope(response, 500, "internal_error")
    assert message == "Internal server error"
    assert "secret-crash-text" not in response.text
    assert "Traceback" not in response.text
    assert "secret-crash-text" in caplog.text


def test_no_error_uses_422_or_detail_shape(client):
    responses = [
        client.post("/test/items", json={}),
        client.post("/test/items", content=b"nope"),
        client.get("/test/range"),
        client.get("/test/range", params={"n": "abc"}),
        client.get("/nope"),
        client.delete("/healthz"),
        client.get("/test/http/400"),
        client.get("/test/crash"),
    ]
    for response in responses:
        assert response.status_code != 422
        assert "detail" not in response.json()
        assert set(response.json()) == {"error"}


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def rollback(self) -> None:
        self.calls.append("rollback")

    def close(self) -> None:
        self.calls.append("close")


@pytest.mark.parametrize("fail", [False, True])
def test_get_session_rolls_back_and_closes(fail):
    app = create_app(Settings(database_url=UNREACHABLE_URL, sweeper_enabled=False))
    sessions: list[FakeSession] = []

    def factory() -> FakeSession:
        sessions.append(FakeSession())
        return sessions[-1]

    app.state.session_factory = factory
    router = APIRouter()

    @router.get("/session")
    def use_session(session: Annotated[Session, Depends(get_session)]) -> dict:
        if fail:
            raise ConflictError("boom")
        return {"ok": True}

    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/session")
        assert response.status_code == (409 if fail else 200)
        client.get("/session")

    assert len(sessions) == 2
    for session in sessions:
        assert session.calls == ["rollback", "close"]


def test_get_session_binds_to_app_engine():
    app = create_app(Settings(database_url=UNREACHABLE_URL))
    session = app.state.session_factory()
    try:
        assert session.get_bind() is app.state.engine
    finally:
        session.close()


def test_ci_red_check():
    assert False, "throwaway red check for #34"
