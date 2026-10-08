import asyncio
import json

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

from nya.core.streaming import handle_streaming_response
from nya.server.nai_admission import UtilityAdmission


class Incoming:
    def __init__(self, body=None, upload_gate=None):
        self.body = (
            body
            or json.dumps(
                {"action": "generate", "input": "test", "parameters": {}}
            ).encode()
        )
        self.reads = 0
        self.disconnect = asyncio.Event()
        self.upload_gate = upload_gate
        self.request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/novelai/ai/generate-image",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "server": ("test", 80),
            },
            self.receive,
        )

    async def receive(self):
        if not self.reads:
            self.reads += 1
            if self.upload_gate:
                await self.upload_gate.wait()
            return {"type": "http.request", "body": self.body, "more_body": False}
        await self.disconnect.wait()
        return {"type": "http.disconnect"}


class Core:
    def __init__(self):
        self.calls = 0
        self.gate = asyncio.Event()

    async def handle_request(self, req):
        self.calls += 1
        await self.gate.wait()
        return Response(b"ok")


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_one_running_two_waiting_fourth_rejected_before_read():
    admission, core = UtilityAdmission(), Core()
    tasks = []
    for _ in range(3):
        tasks.append(
            asyncio.create_task(admission.handle(Incoming().request, core, {}))
        )
        await settle()
    assert core.calls == 1 and admission.admitted == 3
    extra = Incoming()
    assert (await admission.handle(extra.request, core, {})).status_code == 503
    assert extra.reads == 0
    core.gate.set()
    assert all(r.status_code == 200 for r in await asyncio.gather(*tasks))
    assert core.calls == 3
    assert (admission.admitted, admission.uploading, admission.waiting_bytes) == (
        0,
        0,
        0,
    )


@pytest.mark.asyncio
async def test_configurable_waiting_limit_zero_rejects_second_before_read():
    admission, core = UtilityAdmission(), Core()
    settings = {"max_waiting_requests": 0}
    active = asyncio.create_task(admission.handle(Incoming().request, core, settings))
    await settle()
    assert core.calls == 1 and admission.admitted == 1

    extra = Incoming()
    assert (await admission.handle(extra.request, core, settings)).status_code == 503
    assert extra.reads == 0

    core.gate.set()
    assert (await active).status_code == 200
    assert admission.admitted == 0


@pytest.mark.asyncio
async def test_two_uploads_and_reserved_byte_budget_before_read():
    admission, core = UtilityAdmission(), Core()
    gate = asyncio.Event()
    settings = {"max_body_bytes": 32 * 1024 * 1024}
    uploads = [Incoming(upload_gate=gate) for _ in range(2)]
    tasks = [
        asyncio.create_task(admission.handle(i.request, core, settings))
        for i in uploads
    ]
    await settle()
    assert admission.waiting_bytes == 64 * 1024 * 1024
    extra = Incoming()
    assert (await admission.handle(extra.request, core, settings)).status_code == 503
    assert extra.reads == 0
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert (admission.admitted, admission.uploading, admission.waiting_bytes) == (
        0,
        0,
        0,
    )
    # Exact waiting-memory threshold independent of count/upload slots.
    admission.waiting_bytes = 64 * 1024 * 1024 + 1
    assert (await admission.handle(extra.request, core, settings)).status_code == 503
    assert extra.reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["timeout", "disconnect", "cancel"])
async def test_waiter_exit_never_dispatches_and_releases(exit_kind):
    admission, core = UtilityAdmission(), Core()
    active = asyncio.create_task(admission.handle(Incoming().request, core, {}))
    await settle()
    incoming = Incoming()
    waiting = asyncio.create_task(
        admission.handle(incoming.request, core, {"queue_wait_seconds": 0.02})
    )
    await settle()
    if exit_kind == "disconnect":
        incoming.disconnect.set()
    elif exit_kind == "cancel":
        waiting.cancel()
    result = await asyncio.gather(waiting, return_exceptions=True)
    if exit_kind == "timeout":
        assert result[0].status_code == 504
    if exit_kind == "disconnect":
        assert result[0].status_code == 499
    assert core.calls == 1 and admission.admitted == 1 and admission.waiting_bytes == 0
    core.gate.set()
    await active
    assert admission.admitted == 0 and not admission.execution.locked()


@pytest.mark.asyncio
async def test_disconnect_during_dispatch_cancels_execution():
    admission, core, incoming = UtilityAdmission(), Core(), Incoming()
    task = asyncio.create_task(admission.handle(incoming.request, core, {}))
    await settle()
    incoming.disconnect.set()
    assert (await task).status_code == 499
    assert admission.admitted == 0 and not admission.execution.locked()


@pytest.mark.asyncio
async def test_stream_slot_held_until_close_even_before_iterator_started():
    admission = UtilityAdmission()

    class StreamingCore:
        async def handle_request(self, req):
            return await handle_streaming_response(httpx.Response(200, content=b"zip"))

    response = await admission.handle(Incoming().request, StreamingCore(), {})
    assert admission.admitted == 1 and admission.execution.locked()
    await response._nya_close()
    await response._nya_close()
    assert admission.admitted == 0 and not admission.execution.locked()
