import httpx
import pytest
from fastapi import Request

from nya.core.streaming import detect_streaming_content
from nya.server.nai_utility import ENTRYPOINT, adapt_request, resolve_endpoint


def test_sse_with_content_length_is_streaming():
    assert detect_streaming_content(
        httpx.Headers(
            {
                "Content-Type": "text/event-stream; charset=utf-8",
                "Content-Length": "100",
            }
        )
    )


@pytest.mark.parametrize(
    "payload,accept",
    [
        ([], ""),
        ({"parameters": []}, ""),
        ({"action": "generate", "input": "test", "parameters": {"stream": "bad"}}, ""),
        (
            {
                "image": "test",
                "parameters": {"information_extracted": 1, "stream": "sse"},
            },
            "text/event-stream",
        ),
    ],
)
def test_unsupported_payloads(payload, accept):
    with pytest.raises(ValueError):
        resolve_endpoint(payload, accept)


@pytest.mark.asyncio
async def test_chunked_body_limit():
    chunks = iter(
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ]
    )

    async def receive():
        return next(chunks)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": ENTRYPOINT,
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "scheme": "http",
            "server": ("localhost", 80),
            "client": ("127.0.0.1", 1),
        },
        receive,
    )
    with pytest.raises(OverflowError):
        await adapt_request(request, 5)
