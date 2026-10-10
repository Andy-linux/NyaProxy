"""Opt-in, exact NovelAI business routes using the existing HTTP executor.

No generic queue/retry/rotation policy is inherited by these paid operations.
"""

import asyncio
import hashlib
import json
import time
import zlib
from types import SimpleNamespace

import httpx
from starlette.responses import JSONResponse

from ..common.models import ProxyRequest
from .nai_admission import UtilityAdmission
from .nai_utility import adapt_request

ROOT = "/api/novelai"
SUBSCRIPTION_FIELDS = (
    "tier",
    "active",
    "expiresAt",
    "trainingStepsLeft",
    "trainingStepsLeft.fixedTrainingStepsLeft",
    "trainingStepsLeft.purchasedTrainingSteps",
    "usage.isNegative",
    "usage.percent",
    "usage.timeUntilNextPercent",
)
INFORMATION_FIELDS = ("trialImagesLeft", "trialActionsLeft", "banStatus", "banMessage")
SAFE_ACCOUNT_FIELDS = frozenset(
    INFORMATION_FIELDS
    + SUBSCRIPTION_FIELDS
    + tuple("subscription." + field for field in SUBSCRIPTION_FIELDS)
)
ROUTES = {
    "/ai/generate-image": ("POST", "image", "generation"),
    "/ai/generate-image-stream": ("POST", "image", "generation"),
    "/ai/encode-vibe": ("POST", "image", "generation"),
    "/ai/upscale": ("POST", "image", "upscale"),
    "/ai/augment-image": ("POST", "image", "upscale"),
    "/user/information": ("GET", "account", "account"),
    "/user/data": ("GET", "account", "account"),
    "/user/subscription": ("GET", "account", "account"),
    "/oa/v1/chat/completions": ("POST", "text", "text"),
    # Optional suggest-tags is not exposed until its exact query contract is verified.
}


async def native_request(request, limit):
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise ValueError("Encoded request bodies are not supported")
    if request.url.query:
        raise ValueError("Query parameters are not supported")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise OverflowError
        body.extend(chunk)
    if request.method == "GET" and body:
        raise ValueError("GET body is not supported")
    if request.method == "POST":
        content_type = request.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type == "application/json":
            try:
                if not isinstance(json.loads(body), dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                raise ValueError("Expected a JSON object") from None
        elif (
            media_type == "multipart/form-data" and "boundary=" in content_type.lower()
        ):
            pass  # Preserve official FormData boundary and opaque upload bytes.
        else:
            raise ValueError("Content-Type must be JSON or multipart with boundary")
    req = ProxyRequest(
        request.method, request.url, httpx.Headers(request.headers), bytes(body)
    )
    req.nai_utility = True
    req._nai_accept = request.headers.get("accept", "*/*")
    req._nai_content_type = request.headers.get("content-type", "application/json")
    return req


class NativeGateway:
    def __init__(self):
        self.admissions = {}
        self.read_active = 0
        self.read_last = {}

    def binding(self, config, credential):
        keys = config.get_api_variable_values(
            "novelai", config.get_api_key_variable("novelai")
        )
        if not keys or any(not isinstance(key, str) or not key for key in keys):
            raise ValueError("NovelAI upstream keys are unavailable")
        # Stable across processes/restarts; never random A-query/B-generate.
        index = int.from_bytes(
            hashlib.sha256(credential.encode()).digest(), "big"
        ) % len(keys)
        return keys[index]

    async def handle(self, request, core, settings):
        path = request.url.path[len(ROOT) :]
        route = ROUTES.get(path)
        if route is None:
            return JSONResponse({"error": "Not found"}, 404)
        method, target, business = route
        if request.method != method:
            return JSONResponse(
                {"error": "Method not allowed"}, 405, headers={"Allow": method}
            )
        credential = request.headers["authorization"].removeprefix("Bearer ").strip()
        try:
            key = self.binding(core.config, credential)
        except ValueError:
            return JSONResponse({"error": "Upstream binding unavailable"}, 503)
        timeout = float(
            settings.get(
                f"{business}_timeout_seconds",
                {"generation": 300, "upscale": 300, "text": 120, "account": 15}[
                    business
                ],
            )
        )
        selected = dict(
            settings,
            total_timeout_seconds=timeout
            + float(settings.get("queue_wait_seconds", 120)),
        )

        async def execute(req):
            actual_path = req._url.path[len(ROOT) :]
            actual_target = ROUTES[actual_path][1]
            endpoint = settings["upstreams"][actual_target].rstrip("/")
            business_deadline = asyncio.get_running_loop().time() + timeout
            req._nai_timeout = timeout
            req.api_name = "novelai"
            req.api_key = key
            req.trail_path = actual_path
            req.url = endpoint + actual_path
            req.headers = httpx.Headers(
                {
                    "Authorization": f"Bearer {key}",
                    "Accept": getattr(
                        req,
                        "_nai_accept",
                        "text/event-stream"
                        if actual_path.endswith("-stream")
                        else "*/*",
                    ),
                }
            )
            req.headers["Accept-Encoding"] = "identity"
            if req.method == "POST":
                req.headers["Content-Type"] = getattr(
                    req, "_nai_content_type", "application/json"
                )
            try:
                async with asyncio.timeout_at(business_deadline):
                    response = await core.request_executor.execute(req)
                response._nya_deadline = business_deadline
                response.raw_headers = [
                    (name, value)
                    for name, value in response.raw_headers
                    if name
                    in {
                        b"content-type",
                        b"content-encoding",
                        b"cache-control",
                        b"retry-after",
                        b"x-accel-buffering",
                    }
                ]
                return response
            except (TimeoutError, httpx.TimeoutException):
                return JSONResponse(
                    {"error": "Upstream timeout; paid outcome may be unknown"}, 504
                )
            except httpx.TransportError:
                return JSONResponse(
                    {
                        "error": "Upstream transport failure; paid outcome may be unknown"
                    },
                    502,
                )

        if business != "account":
            admission = self.admissions.setdefault(key, UtilityAdmission())
            # Preserve old single-entry payload classification only when enabled.
            adapter = (
                adapt_request
                if path == "/ai/generate-image"
                and settings.get("legacy_entrypoint", True)
                and request.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
                == "application/json"
                else native_request
            )
            return await admission.handle(
                request,
                SimpleNamespace(handle_request=execute),
                selected,
                adapter=adapter,
            )
        # Read-only traffic has independent bounded concurrency and per-binding
        # frequency control; no cache and no fabricated zero balances.
        now = time.monotonic()
        rate_key = (key, path)
        if self.read_active >= int(settings.get("read_max_concurrency", 4)):
            return JSONResponse({"error": "Read capacity full"}, 429)
        interval = float(settings.get("read_min_interval_seconds", 1))
        if now - self.read_last.get(rate_key, -float("inf")) < interval:
            return JSONResponse(
                {"error": "Read rate limit"},
                429,
                headers={"Retry-After": str(max(1, int(interval)))},
            )
        self.read_last[rate_key] = now
        self.read_active += 1
        response = None
        handed_off = False
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            async with asyncio.timeout(timeout):
                req = await native_request(
                    request, int(settings.get("max_body_bytes", 33554432))
                )
                response = await execute(req)
                if target == "account":
                    if response.status_code != 200:
                        return JSONResponse(
                            {
                                "error": "Account query unavailable",
                                "upstream_status": response.status_code,
                            },
                            response.status_code,
                        )
                    # Account projection is operator-explicit and recursively
                    # leaf-only: never forward whole personal-data objects.
                    raw = bytearray()
                    limit = int(settings.get("max_account_response_bytes", 65536))
                    encoding = response.headers.get(
                        "content-encoding", "identity"
                    ).lower()
                    if encoding not in ("identity", "gzip"):
                        return JSONResponse(
                            {"error": "Unsupported account response encoding"}, 502
                        )
                    decoder = (
                        zlib.decompressobj(16 + zlib.MAX_WBITS)
                        if encoding == "gzip"
                        else None
                    )
                    wire_bytes = 0
                    try:
                        async for chunk in response.body_iterator:
                            wire_bytes += len(chunk)
                            if wire_bytes > limit:
                                raise OverflowError
                            decoded = (
                                decoder.decompress(chunk, limit - len(raw) + 1)
                                if decoder
                                else chunk
                            )
                            if len(raw) + len(decoded) > limit or (
                                decoder and decoder.unconsumed_tail
                            ):
                                raise OverflowError
                            raw.extend(decoded)
                        if decoder and (not decoder.eof or decoder.unused_data):
                            return JSONResponse(
                                {"error": "Invalid compressed account response"}, 502
                            )
                    except zlib.error:
                        return JSONResponse(
                            {"error": "Invalid compressed account response"}, 502
                        )
                    try:
                        data = json.loads(raw)
                        if not isinstance(data, dict):
                            raise ValueError
                    except (ValueError, UnicodeDecodeError):
                        return JSONResponse(
                            {"error": "Invalid upstream account response"}, 502
                        )
                    projected = {}
                    defaults = (
                        SUBSCRIPTION_FIELDS
                        if path == "/user/subscription"
                        else INFORMATION_FIELDS
                        if path == "/user/information"
                        else tuple(
                            "subscription." + field for field in SUBSCRIPTION_FIELDS
                        )
                    )
                    for field in settings.get("account_fields", defaults):
                        if field not in SAFE_ACCOUNT_FIELDS:
                            continue
                        source = data
                        parts = field.split(".")
                        for part in parts:
                            source = (
                                source.get(part) if isinstance(source, dict) else None
                            )
                        if source is not None and isinstance(
                            source, (str, int, float, bool)
                        ):
                            destination = projected
                            for part in parts[:-1]:
                                if not isinstance(destination.get(part), dict):
                                    destination[part] = {}
                                destination = destination[part]
                            destination[parts[-1]] = source
                    return JSONResponse(
                        projected, headers={"Cache-Control": "no-store"}
                    )
                response._nya_deadline = deadline
                finalizer = getattr(response, "_nya_add_finalizer", None)
                if finalizer:
                    handed_off = True

                    def release_read():
                        self.read_active -= 1

                    finalizer(release_read)
                return response
        except TimeoutError:
            return JSONResponse({"error": "Read timeout"}, 504)
        except OverflowError:
            return JSONResponse({"error": "Account response too large"}, 502)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "Invalid read request"}, 400)
        except httpx.TransportError:
            return JSONResponse({"error": "Account response transport failure"}, 502)
        finally:
            if not handed_off:
                self.read_active -= 1
            if response is not None and not handed_off:
                close = getattr(response, "_nya_close", None)
                if close:
                    await close()
