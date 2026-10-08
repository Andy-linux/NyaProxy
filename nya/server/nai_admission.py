"""Single-process, bounded admission for the dedicated NovelAI gateway."""

import asyncio
from contextlib import suppress

from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from .nai_utility import adapt_request


class UtilityAdmission:
    """Upload reservations, waiting bodies and execution have separate lifetimes.

    All counter changes are synchronous on the application's single event loop.
    Unknown-length uploads reserve the entire per-body limit before reading.
    """

    def __init__(self):
        self.admitted = 0
        self.uploading = 0
        self.waiting_bytes = 0
        self.execution = asyncio.Semaphore(1)

    async def handle(self, request, core, settings):
        limit = int(settings.get("max_body_bytes", 32 * 1024 * 1024))
        budget = int(settings.get("max_waiting_body_bytes", 96 * 1024 * 1024))
        max_waiting_requests = int(settings.get("max_waiting_requests", 2))
        # Never trust Content-Length to reserve less memory: chunked and dishonest
        # senders get the same hard cap, and no request body is read on rejection.
        if (
            self.admitted >= 1 + max_waiting_requests
            or self.uploading >= 2
            or self.waiting_bytes + limit > budget
        ):
            return JSONResponse({"error": "NovelAI queue or upload capacity full"}, 503)
        self.admitted += 1
        self.uploading += 1
        self.waiting_bytes += limit
        reserved = limit
        uploading = True
        executing = False
        handed_off = False
        req = None
        watcher = None
        work = None
        deadline = asyncio.get_running_loop().time() + float(
            settings.get("total_timeout_seconds", 25)
        )

        def release():
            nonlocal executing, handed_off
            if executing:
                executing = False
                self.execution.release()
            if handed_off:
                handed_off = False
                self.admitted -= 1

        async def disconnected():
            # Body is fully consumed before this task becomes the sole receive
            # consumer. Stop it before StreamingResponse starts receiving.
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    return

        async def dispatch():
            nonlocal reserved, executing
            async with asyncio.timeout(float(settings.get("queue_wait_seconds", 10))):
                await self.execution.acquire()
            executing = True
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            self.waiting_bytes -= reserved
            reserved = 0
            return await core.handle_request(req)

        try:
            async with asyncio.timeout_at(deadline):
                async with asyncio.timeout(
                    float(settings.get("upload_timeout_seconds", 10))
                ):
                    req = await adapt_request(request, limit)
                req._nai_deadline = deadline
                self.uploading -= 1
                uploading = False
                self.waiting_bytes -= reserved - len(req.content)
                reserved = len(req.content)
                watcher = asyncio.create_task(disconnected())
                work = asyncio.create_task(dispatch())
                done, _ = await asyncio.wait(
                    (watcher, work), return_when=asyncio.FIRST_COMPLETED
                )
                if watcher in done:
                    raise ClientDisconnect()
                response = work.result()
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher
                req.content = None
                add_finalizer = getattr(response, "_nya_add_finalizer", None)
                if add_finalizer:
                    handed_off = True
                    add_finalizer(release)
                    response._nya_deadline = deadline
                return response
        except OverflowError:
            return JSONResponse({"error": "Request body too large"}, 413)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, 400)
        except TimeoutError:
            return JSONResponse(
                {"error": "NovelAI upload, queue or request deadline exceeded"}, 504
            )
        except ClientDisconnect:
            return JSONResponse({"error": "Client disconnected"}, 499)
        finally:
            for task in (watcher, work):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (watcher, work) if task is not None),
                return_exceptions=True,
            )
            try:
                # If cancellation raced with response creation, close the orphan.
                if (
                    not handed_off
                    and work is not None
                    and not work.cancelled()
                    and work.done()
                ):
                    if work.exception() is None:
                        close = getattr(work.result(), "_nya_close", None)
                        if close:
                            await close()
            finally:
                if req is not None:
                    req.content = None
                if uploading:
                    self.uploading -= 1
                self.waiting_bytes -= reserved
                if not handed_off:
                    release()
                    self.admitted -= 1
