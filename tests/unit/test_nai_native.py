import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from nya.server.nai_native import NativeGateway, native_request


def request(method="POST", body=b"{}", query=b"", encoding=b"identity"):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/api/novelai/ai/upscale",
            "query_string": query,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-encoding", encoding),
            ],
            "server": ("localhost", 80),
            "scheme": "http",
        },
        receive,
    )


@pytest.mark.asyncio
async def test_native_bytes_unmodified():
    body = json.dumps(
        {"parameters": {"unrecognized_business_field": [1, 2]}}, indent=2
    ).encode()
    req = await native_request(request(body=body), 1024)
    assert req.content == body
    assert req.nai_utility


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,body,query,encoding,error",
    [
        ("GET", b"{}", b"", b"identity", ValueError),
        ("POST", b"[]", b"", b"identity", ValueError),
        ("POST", b"{}", b"host=private", b"identity", ValueError),
        ("POST", b"{}", b"", b"gzip", ValueError),
        ("POST", b"123456", b"", b"identity", OverflowError),
    ],
)
async def test_native_invalid_requests(method, body, query, encoding, error):
    with pytest.raises(error):
        await native_request(request(method, body, query, encoding), 4)


def test_binding_is_deterministic_and_validates_keys():
    config = SimpleNamespace(
        get_api_variable_values=lambda *_: ["a", "b", "c"],
        get_api_key_variable=lambda _: "keys",
    )
    first = NativeGateway().binding(config, "synthetic-downstream")
    assert first in ("a", "b", "c")
    assert NativeGateway().binding(config, "synthetic-downstream") == first
    config.get_api_variable_values = lambda *_: []
    with pytest.raises(ValueError):
        NativeGateway().binding(config, "synthetic-downstream")
