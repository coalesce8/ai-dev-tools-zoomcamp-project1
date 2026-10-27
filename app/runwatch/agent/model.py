"""The model seam: a small protocol the loop depends on, and the real wrapper.

The loop never sees `anthropic` types. `AnthropicModelClient` turns an SDK
reply into a `ModelResponse` of plain dicts, and every SDK failure into a
`ModelApiError` that carries only a status and an exception class name, never
the request body or the API key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
import httpx2


class ModelApiError(Exception):
    """A model request failed. The message holds only `kind` and `status`."""

    def __init__(self, kind: str, status: int | None = None) -> None:
        super().__init__(f"model API error: kind={kind} status={status}")
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total(self) -> int:
        """The size of the conversation so far; each token counts once."""
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
            + self.output_tokens
        )


@dataclass(frozen=True)
class ModelResponse:
    stop_reason: str | None
    # Plain dicts: {"type": "text", "text"} or {"type": "tool_use", "id",
    # "name", "input"}.
    content: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


class ModelClient(Protocol):
    def create(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> ModelResponse: ...


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _block(raw: Any) -> dict[str, Any] | None:
    kind = getattr(raw, "type", None)
    if kind == "text":
        return {"type": "text", "text": getattr(raw, "text", "") or ""}
    if kind == "tool_use":
        return {
            "type": "tool_use",
            "id": raw.id,
            "name": raw.name,
            "input": raw.input,
        }
    return None  # thinking and other kinds are not used here


class AnthropicModelClient:
    def __init__(
        self,
        api_key: str,
        timeout_s: float,
        http_client: httpx2.Client | None = None,
    ) -> None:
        # max_retries=0: "3 failures in a row" is the loop's rule, so the SDK
        # must not retry on its own.
        self._client = anthropic.Anthropic(
            api_key=api_key,
            max_retries=0,
            timeout=timeout_s,
            http_client=http_client,
        )

    def __repr__(self) -> str:
        return "AnthropicModelClient()"

    def create(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> ModelResponse:
        try:
            reply = self._client.messages.create(
                model=model,
                system=system,
                tools=tools,
                messages=messages,
                max_tokens=max_tokens,
            )
            usage = getattr(reply, "usage", None)
            return ModelResponse(
                stop_reason=reply.stop_reason,
                content=[
                    b for b in (_block(raw) for raw in reply.content) if b is not None
                ],
                usage=Usage(
                    input_tokens=_count(getattr(usage, "input_tokens", 0)),
                    output_tokens=_count(getattr(usage, "output_tokens", 0)),
                    cache_creation_input_tokens=_count(
                        getattr(usage, "cache_creation_input_tokens", 0)
                    ),
                    cache_read_input_tokens=_count(
                        getattr(usage, "cache_read_input_tokens", 0)
                    ),
                ),
            )
        except anthropic.APIStatusError as exc:
            raise ModelApiError(type(exc).__name__, exc.status_code) from None
        except (anthropic.AnthropicError, httpx2.HTTPError) as exc:
            # Timeouts and connection failures: no status.
            raise ModelApiError(type(exc).__name__, None) from None
