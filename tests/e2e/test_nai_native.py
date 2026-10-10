"""Actual loopback HTTP and subprocess tests; no official services contacted."""

import asyncio
import concurrent.futures
import gzip
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from nya.server.nai_native import ROOT, ROUTES
from tests.e2e.conftest import get_free_port, wait_for_http

pytestmark = pytest.mark.e2e
BINARY = b"\x00\x00\x00\x04\x81\xa1x\x01"


@pytest.fixture
def native_gateway(tmp_path, request):
    variant = getattr(request, "param", None)
    use_defaults = variant == "defaults"
    invalid_account = getattr(request, "param", None) == "invalid"
    records = []
    app = FastAPI()
    app.state.invalid_account = invalid_account

    @app.api_route("/{path:path}", methods=["POST", "GET"])
    async def handle(request: Request, path: str):
        body = await request.body()
        payload = (
            json.loads(body)
            if body
            and request.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        records.append((path, body, dict(request.headers), time.monotonic()))
        if path == "health":
            return {}
        if variant in (
            "account-gzip",
            "account-bomb",
            "account-truncated",
        ) and path.startswith("user/"):
            raw = json.dumps(
                {
                    "subscription": {
                        "tier": 3,
                        "trainingStepsLeft": {"fixedTrainingStepsLeft": 100},
                    },
                    "private": "x" * (100000 if variant == "account-bomb" else 1),
                }
            ).encode()
            compressed = gzip.compress(raw)
            if variant == "account-truncated":
                compressed = compressed[:-5]
            return Response(
                compressed,
                media_type="application/json",
                headers={"Content-Encoding": "gzip"},
            )
        if payload.get("input") == "headers-slow":
            await asyncio.sleep(0.35)
        if getattr(request.app.state, "invalid_account", False) and path.startswith(
            "user/"
        ):
            return Response(b"invalid-json", media_type="application/json")
        if use_defaults and path == "user/subscription":
            return JSONResponse(
                {
                    "tier": 3,
                    "active": True,
                    "trainingStepsLeft": 0,
                    "usage": {
                        "isNegative": True,
                        "percent": 0,
                        "timeUntilNextPercent": 10,
                    },
                    "accessToken": "never-forward",
                    "email": "private",
                }
            )
        if use_defaults and path == "user/information":
            return JSONResponse(
                {
                    "trialImagesLeft": 0,
                    "banStatus": False,
                    "plaintextEmail": "private",
                    "marketing": True,
                }
            )
        if path.startswith("user/"):
            return JSONResponse(
                {
                    "subscription": {
                        "tier": 3,
                        "trainingStepsLeft": {"fixedTrainingStepsLeft": 100},
                    },
                    "email": "private@example.invalid",
                    "accessToken": "never-forward",
                }
            )
        if payload.get("input") == "error":
            return Response(b'{ "error": "quota" }', 402, media_type="application/json")
        if payload.get("input") == "retry":
            return Response(b"busy", 429)
        if payload.get("input") == "timeout":
            await asyncio.sleep(2)
        if payload.get("input") == "gzip":
            return Response(
                gzip.compress(BINARY),
                media_type="application/octet-stream",
                headers={
                    "content-encoding": "gzip",
                    "set-cookie": "private=1",
                    "x-private": "secret",
                },
            )
        if path.endswith("-stream") or payload.get("input") == "hold":

            async def chunks():
                yield BINARY[:4]
                await asyncio.sleep(0.6)
                yield BINARY[4:]

            return StreamingResponse(chunks(), media_type="application/x-msgpack")
        return Response(BINARY, media_type="application/octet-stream")

    upstream_port = get_free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=upstream_port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{upstream_port}"
    wait_for_http(origin + "/health")
    config = yaml.safe_load(Path("configs/novelai-utility.yaml").read_text())
    config["server"]["api_key"] = ["admin-synthetic", "client-synthetic"]
    config["server"]["nai_utility"].update(
        native_enabled=True,
        legacy_entrypoint=False,
        upstreams=dict(image=origin, account=origin, text=origin),
        account_fields=[
            "subscription.tier",
            "subscription.trainingStepsLeft.fixedTrainingStepsLeft",
        ],
        read_min_interval_seconds=0,
        generation_timeout_seconds=1,
        queue_wait_seconds=0.8,
    )
    if use_defaults:
        config["server"]["nai_utility"].pop("account_fields")
    if variant == "strict-deadline":
        config["server"]["nai_utility"].update(
            generation_timeout_seconds=0.2, queue_wait_seconds=1
        )
    config["apis"]["novelai"]["variables"]["keys"] = ["upstream-a", "upstream-b"]
    config["apis"]["novelai"]["endpoint"] = origin
    # Generic policy must not cause unsafe retries or block native exact routes.
    config["default_settings"]["retry"]["enabled"] = True
    config_path = tmp_path / "native.yaml"
    config_path.write_text(yaml.safe_dump(config))
    port = get_free_port()
    log = (tmp_path / "native.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "nya",
            "--config",
            str(config_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-reload",
        ],
        stdout=log,
        stderr=log,
        env=os.environ.copy(),
    )
    url = f"http://127.0.0.1:{port}"
    try:
        wait_for_http(url + "/health")
        with httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer client-synthetic"},
            timeout=5,
            trust_env=False,
        ) as client:
            yield client, records, url
    finally:
        process.terminate()
        process.wait(timeout=5)
        log.close()
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.parametrize("path,route", ROUTES.items())
def test_all_native_routes(native_gateway, path, route):
    client, records, _ = native_gateway
    method = route[0]
    raw = b'{ "input" : "test", "untouched": true }\n'
    response = client.request(
        method,
        ROOT + path,
        content=raw if method == "POST" else b"",
        headers={
            "Content-Type": "application/json",
            "Cookie": "private=1",
            "Origin": "https://private.invalid",
            "X-Device-ID": "personal",
            "X-Forwarded-For": "1.2.3.4",
            "Accept": "application/x-msgpack",
        },
    )
    assert response.status_code == 200
    actual_path, body, headers, _ = records[-1]
    assert actual_path == path.lstrip("/")
    assert body == (raw if method == "POST" else b"")
    assert headers["authorization"] in ("Bearer upstream-a", "Bearer upstream-b")
    assert set(headers) == {
        "authorization",
        "accept",
        "accept-encoding",
        "host",
        "content-length",
    } | ({"content-type"} if method == "POST" else set())
    if path.startswith("/user/"):
        assert response.json() == {
            "subscription": {
                "tier": 3,
                "trainingStepsLeft": {"fixedTrainingStepsLeft": 100},
            }
        }
    else:
        assert response.content == BINARY


def test_canonical_bearer_identity_binding(native_gateway):
    client, records, _ = native_gateway
    bindings = []
    for credential in (
        "Bearer client-synthetic",
        "Bearer   client-synthetic",
        "Bearer    client-synthetic",
    ):
        assert (
            client.get(
                ROOT + "/user/data", headers={"Authorization": credential}
            ).status_code
            == 200
        )
        bindings.append(records[-1][2]["authorization"])
        assert (
            client.post(
                ROOT + "/ai/generate-image",
                json={},
                headers={"Authorization": credential},
            ).status_code
            == 200
        )
        bindings.append(records[-1][2]["authorization"])
    assert len(set(bindings)) == 1


@pytest.mark.parametrize("native_gateway", ["strict-deadline"], indirect=True)
def test_response_headers_strict_business_deadline(native_gateway):
    client, records, _ = native_gateway
    started = time.monotonic()
    assert (
        client.post(
            ROOT + "/ai/generate-image", json={"input": "headers-slow"}
        ).status_code
        == 504
    )
    assert time.monotonic() - started < 0.32
    assert len(records) == 2  # mock health + exactly one paid dispatch
    assert client.post(ROOT + "/ai/generate-image", json={}).status_code == 200


@pytest.mark.parametrize(
    "native_gateway,expected",
    [("account-gzip", 200), ("account-bomb", 502), ("account-truncated", 502)],
    indirect=["native_gateway"],
)
def test_account_gzip_decoding_is_bounded(native_gateway, expected):
    client, _, _ = native_gateway
    response = client.get(ROOT + "/user/data")
    assert response.status_code == expected
    if expected == 200:
        assert response.json() == {
            "subscription": {
                "tier": 3,
                "trainingStepsLeft": {"fixedTrainingStepsLeft": 100},
            }
        }
        assert "content-encoding" not in response.headers


@pytest.mark.parametrize("native_gateway", ["invalid"], indirect=True)
def test_invalid_account_upstream_is_502(native_gateway):
    client, _, _ = native_gateway
    assert client.get(ROOT + "/user/data").status_code == 502
    assert client.get(ROOT + "/user/data?invalid=1").status_code == 400


@pytest.mark.parametrize("native_gateway", ["defaults"], indirect=True)
def test_default_account_projection_and_unknown_semantics(native_gateway):
    client, _, _ = native_gateway
    subscription = client.get(ROOT + "/user/subscription").json()
    assert subscription == {
        "tier": 3,
        "active": True,
        "trainingStepsLeft": 0,
        "usage": {"isNegative": True, "percent": 0, "timeUntilNextPercent": 10},
    }
    information = client.get(ROOT + "/user/information").json()
    assert information == {"trialImagesLeft": 0, "banStatus": False}
    data = client.get(ROOT + "/user/data").json()
    assert data == {
        "subscription": {
            "tier": 3,
            "trainingStepsLeft": {"fixedTrainingStepsLeft": 100},
        }
    }
    assert "usage" not in data["subscription"]


def test_multipart_boundary_bytes_preserved(native_gateway):
    client, records, _ = native_gateway
    raw = b'--synthetic-boundary\r\nContent-Disposition: form-data; name="image"; filename="test.bin"\r\n\r\n\x00\xff\x80\r\n--synthetic-boundary--\r\n'
    content_type = "multipart/form-data; boundary=synthetic-boundary"
    response = client.post(
        ROOT + "/ai/generate-image", content=raw, headers={"Content-Type": content_type}
    )
    assert response.status_code == 200 and response.content == BINARY
    assert records[-1][1] == raw
    assert records[-1][2]["content-type"] == content_type


def test_exact_policy_and_auth(native_gateway):
    client, records, _ = native_gateway
    before = len(records)
    for path in (
        "/user/delete",
        "/user/information/extra",
        "/ai/upscale/",
        "/../config",
        "/oa/v1/chat/completions/extra",
    ):
        assert client.post(ROOT + path, json={}).status_code == 404
    assert client.post(ROOT + "/user/information", json={}).status_code == 405
    assert (
        client.get(
            ROOT + "/user/information", headers={"Authorization": "Bearer invalid"}
        ).status_code
        == 403
    )
    assert client.get("/config").status_code == 404
    assert (
        client.get(ROOT + "/user/information?host=http://127.0.0.1").status_code == 400
    )
    assert len(records) == before


def test_binding_errors_compression_and_no_retry(native_gateway):
    client, records, _ = native_gateway
    client.get(ROOT + "/user/information")
    key = records[-1][2]["authorization"]
    for prompt, status in (
        ("test", 200),
        ("error", 402),
        ("retry", 429),
        ("gzip", 200),
    ):
        before = len(records)
        response = client.post(ROOT + "/ai/generate-image", json={"input": prompt})
        assert response.status_code == status
        assert len(records) == before + 1
        assert records[-1][2]["authorization"] == key
        if prompt == "error":
            assert response.content == b'{ "error": "quota" }'
        if prompt == "gzip":
            assert response.content == BINARY
            assert response.headers["content-encoding"] == "gzip"
            assert (
                "set-cookie" not in response.headers
                and "x-private" not in response.headers
            )


def test_streaming_queue_and_read_independence(native_gateway):
    client, records, _ = native_gateway
    with client.stream("POST", ROOT + "/ai/generate-image-stream", json={}) as response:
        iterator = response.iter_raw()
        started = time.monotonic()
        assert next(iterator) == BINARY[:4]
        assert client.get(ROOT + "/user/information").status_code == 200
        assert time.monotonic() - started < 0.5
        assert b"".join(iterator) == BINARY[4:]
    assert client.post(ROOT + "/ai/generate-image", json={}).status_code == 200


def test_queue_full_cancelled_waiter_never_dispatches(native_gateway):
    client, records, url = native_gateway
    with client.stream("POST", ROOT + "/ai/generate-image-stream", json={}) as response:
        iterator = response.iter_raw()
        assert next(iterator) == BINARY[:4]
        before = len(records)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:

            def cancelled_waiter():
                try:
                    httpx.post(
                        url + ROOT + "/ai/generate-image",
                        json={"input": "cancelled"},
                        headers={"Authorization": "Bearer client-synthetic"},
                        timeout=0.2,
                        trust_env=False,
                    )
                except httpx.TimeoutException:
                    return
                raise AssertionError("Waiter should time out")

            waiter = pool.submit(cancelled_waiter)
            time.sleep(0.05)
            assert client.post(ROOT + "/ai/generate-image", json={}).status_code == 503
            waiter.result()
        assert b"".join(iterator) == BINARY[4:]
        assert len(records) == before
    assert client.post(ROOT + "/ai/generate-image", json={}).status_code == 200


def test_timeout_is_not_retried_and_capacity_recovers(native_gateway):
    client, records, _ = native_gateway
    before = len(records)
    assert (
        client.post(ROOT + "/ai/generate-image", json={"input": "timeout"}).status_code
        == 504
    )
    assert len(records) == before + 1
    assert client.post(ROOT + "/ai/generate-image", json={}).status_code == 200
