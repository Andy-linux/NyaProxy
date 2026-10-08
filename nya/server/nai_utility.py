"""NAI-Utility-Tool single-endpoint compatibility, without payload rewriting."""

import json

from httpx import Headers

from ..common.models import ProxyRequest

ENTRYPOINT = "/api/novelai/ai/generate-image"


def utility_settings(config):
    getter = getattr(config, "get_nai_utility_settings", None)
    return getter() if getter else {}


def resolve_endpoint(payload, accept):
    if not isinstance(payload, dict) or not isinstance(payload.get("parameters"), dict):
        raise ValueError("Expected a JSON object with parameters")
    params = payload["parameters"]
    accepts_sse = any(
        part.split(";", 1)[0].strip().lower() == "text/event-stream"
        for part in accept.split(",")
    )
    if payload.get("action") in ("generate", "img2img", "infill") and isinstance(
        payload.get("input"), str
    ):
        if params.get("stream") not in (None, "sse"):
            raise ValueError("Unsupported stream mode")
        streaming = params.get("stream") == "sse"
        if streaming != accepts_sse:
            raise ValueError("Stream mode mismatch")
        return "/ai/generate-image-stream" if streaming else "/ai/generate-image"
    if (
        "action" not in payload
        and isinstance(payload.get("image"), str)
        and "information_extracted" in params
        and not accepts_sse
        and "stream" not in params
    ):
        return "/ai/encode-vibe"
    raise ValueError("Unsupported NovelAI operation")


async def adapt_request(request, max_body_bytes):
    if request.method != "POST":
        raise ValueError("POST required")
    if request.url.query:
        raise ValueError("Query parameters are not supported")
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise ValueError("Content-Type must be application/json")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise ValueError("Encoded request bodies are not supported")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_body_bytes:
            raise OverflowError
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Invalid JSON") from None
    endpoint = resolve_endpoint(payload, request.headers.get("accept", ""))
    req = ProxyRequest(
        method="POST",
        _url=request.url.replace(path=f"/api/novelai{endpoint}"),
        headers=Headers(request.headers),
        content=bytes(body),
        ip=request.client.host if request.client else None,
    )
    req.nai_utility = True
    return req
