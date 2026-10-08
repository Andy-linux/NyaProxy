"""Real socket tests: client -> NyaProxy queue -> recording upstream."""

import asyncio
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
from starlette.responses import Response, StreamingResponse

from nya.server.nai_utility import ENTRYPOINT
from tests.e2e.conftest import get_free_port, wait_for_http

ZIP_BYTES = b"PK\x03\x04\x00\xff\x80mock-image-zip"
VIBE_BYTES = bytes(range(256))


@pytest.fixture
def utility_gateway(tmp_path):
    records = []
    upstream = FastAPI()

    @upstream.post("/{path:path}")
    async def handle(request: Request, path: str):
        body = await request.body()
        records.append((path, body, dict(request.headers)))
        payload = json.loads(body)
        if payload.get("input") == "redirect":
            return Response(status_code=307, headers={"location": "/should-not-follow"})
        if payload.get("input") == "error":
            return Response(
                b'{ "error": "quota" }', status_code=402, media_type="application/json"
            )
        if payload.get("input") == "gzip":
            return Response(
                gzip.compress(ZIP_BYTES),
                media_type="application/zip",
                headers={"content-encoding": "gzip"},
            )
        if path.endswith("stream"):

            async def events():
                yield b"data: first\n\n"
                await asyncio.sleep(0.5)
                yield b"data: second\n\n"

            return StreamingResponse(events(), media_type="text/event-stream")
        return Response(
            VIBE_BYTES if path.endswith("vibe") else ZIP_BYTES,
            media_type="application/octet-stream",
        )

    upstream_port = get_free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            upstream, host="127.0.0.1", port=upstream_port, log_level="error"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_for_http(f"http://127.0.0.1:{upstream_port}/health")
    config = yaml.safe_load(Path("configs/novelai-utility.yaml").read_text())
    config["server"]["api_key"] = ["admin-secret", "client-secret"]
    config["server"]["nai_utility"]["max_body_bytes"] = 4096
    config["apis"]["novelai"]["endpoint"] = f"http://127.0.0.1:{upstream_port}"
    config["apis"]["novelai"]["variables"]["keys"] = ["upstream-secret"]
    config["apis"]["novelai"]["headers"]["X-Configured-ID"] = "must-not-leak"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    port = get_free_port()
    log = (tmp_path / "proxy.log").open("w")
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
            headers={"Authorization": "Bearer client-secret"},
            timeout=5,
            trust_env=False,
        ) as client:
            yield client, records
    finally:
        process.terminate()
        process.wait(timeout=5)
        log.close()
        server.should_exit = True
        thread.join(timeout=5)


def generate(action="generate", prompt="test", stream=False):
    return {
        "action": action,
        "input": prompt,
        "model": "nai-diffusion-4-5-full",
        "parameters": {"stream": "sse"} if stream else {},
    }


@pytest.mark.parametrize("action", ["generate", "img2img", "infill"])
def test_generation_bytes_and_headers(utility_gateway, action):
    client, records = utility_gateway
    raw = json.dumps(generate(action), indent=2).encode() + b"\n"
    response = client.post(
        ENTRYPOINT,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Cookie": "device=personal",
            "User-Agent": "private-device",
            "X-Forwarded-For": "1.2.3.4",
            "X-Device-ID": "private",
            "Origin": "https://private.example",
            "Referer": "https://private.example",
        },
    )
    assert response.content == ZIP_BYTES
    path, body, headers = records[-1]
    assert path == "ai/generate-image" and body == raw
    assert headers["authorization"] == "Bearer upstream-secret"
    assert set(headers) == {
        "authorization",
        "content-type",
        "accept",
        "host",
        "content-length",
    }
    assert "client-secret" not in str(headers)


def test_vibe_bytes(utility_gateway):
    client, records = utility_gateway
    response = client.post(
        ENTRYPOINT,
        json={
            "image": "base64",
            "model": "nai-diffusion-4-5-full",
            "parameters": {"information_extracted": 0.6},
        },
    )
    assert response.content == VIBE_BYTES
    assert records[-1][0] == "ai/encode-vibe"


def test_sse_arrives_before_completion(utility_gateway):
    client, records = utility_gateway
    started = time.monotonic()
    with client.stream(
        "POST",
        ENTRYPOINT,
        json=generate(stream=True),
        headers={"Accept": "text/event-stream"},
    ) as response:
        iterator = response.iter_raw()
        first = next(iterator)
        first_at = time.monotonic() - started
        rest = b"".join(iterator)
        finished_at = time.monotonic() - started
        assert response.headers["content-type"].startswith("text/event-stream")
        assert first == b"data: first\n\n" and rest == b"data: second\n\n"
        assert finished_at - first_at > 0.3
    assert records[-1][0] == "ai/generate-image-stream"
    assert records[-1][2]["accept"] == "text/event-stream"
    # The same exclusive upstream key must have been released after the stream.
    assert client.post(ENTRYPOINT, json=generate()).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/info",
        "/metrics",
        "/docs",
        "/config/ui",
        "/dashboard",
        "/api/novelai/ai/encode-vibe",
        "/api/nai/ai/generate-image",
        "/api/novelai/user/data",
    ],
)
def test_other_surfaces_closed(utility_gateway, path):
    client, records = utility_gateway
    assert client.post(path, json=generate()).status_code == 404
    assert records == []


def test_auth_method_invalid_and_limit(utility_gateway):
    client, records = utility_gateway
    assert (
        client.post(
            ENTRYPOINT, json=generate(), headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            ENTRYPOINT, json=generate(), headers={"Authorization": "client-secret"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            ENTRYPOINT,
            json=generate(),
            headers={"Authorization": "", "Cookie": "nyaproxy_api_key=admin-secret"},
        ).status_code
        == 403
    )
    assert client.get(ENTRYPOINT).status_code == 405
    assert (
        client.post(
            ENTRYPOINT, content=b"bad", headers={"Content-Type": "application/json"}
        ).status_code
        == 400
    )
    assert client.post(ENTRYPOINT, json=generate(stream=True)).status_code == 400
    assert (
        client.post(
            ENTRYPOINT, json={"action": "account-delete", "parameters": {}}
        ).status_code
        == 400
    )
    assert (
        client.post(ENTRYPOINT + "?identity=private", json=generate()).status_code
        == 400
    )
    assert (
        client.post(
            ENTRYPOINT,
            content=b"x" * 4097,
            headers={"Content-Type": "application/json"},
        ).status_code
        == 413
    )
    assert records == []


def test_status_compression_and_redirect_passthrough(utility_gateway):
    client, records = utility_gateway
    response = client.post(ENTRYPOINT, json=generate(prompt="error"))
    assert response.status_code == 402 and response.content == b'{ "error": "quota" }'
    response = client.post(ENTRYPOINT, json=generate(prompt="gzip"))
    assert (
        response.headers["content-encoding"] == "gzip" and response.content == ZIP_BYTES
    )
    response = client.post(ENTRYPOINT, json=generate(prompt="redirect"))
    assert response.status_code == 307
    assert len(records) == 3
