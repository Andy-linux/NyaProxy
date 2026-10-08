# Bounded NovelAI gateway verification

Date: 2026-10-08. Base repository: https://github.com/Andy-linux/NyaProxy,
base commit `f915f3267b7668fbd8226f333cd12522c803a93f`.
The repository already exists; this update is a source change, not a server deployment.

## Scope

- Dedicated NovelAI entrypoint: one executing request and up to four waiting/uploading requests.
- Admission happens before body reads; two upload slots, 32 MiB per-body limit,
  96 MiB reserved/retained waiting-body budget. Large bodies can exhaust the byte
  budget before all four waiting positions are usable.
- Upload and queue each have a 10-second limit inside one absolute 25-second
  deadline, including response transmission. The client's existing 30-second
  total timeout remains unchanged and is not extended by heartbeats.
- Raw incremental forwarding includes ZIP with Content-Length, SSE, Vibe,
  compressed responses and upstream errors. No changes to generation parameters.
- Cancellation/expiry prevents subsequent upstream dispatch; active cancellation
  closes upstream work, releases locks/slots, and leaves the worker usable.
- No real NovelAI key, real generation, production deployment, other service
  changes, paid parallelism or Nginx installation was used for verification.

## Automated checks

Final source candidate: **425 tests passed** (125.48 seconds). Ruff lint and
format checks passed, mypy passed for its configured 14-file ratchet, and the
example configuration validated. One existing pytest warning is noted in the
test output; no failing tests.

Run from the source checkout:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv sync --extra dev --extra lint
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/python -m nya --config configs/novelai-utility.yaml --check-config
```

Added coverage includes capacity rejection before body reads, two-upload admission,
96 MiB reservation threshold, cancelled/expired/disconnected waiters, active cancellation
and reusable workers, response finalization before iteration, shielded close on disconnect,
and incrementally delivered ZIP responses even when upstream supplies Content-Length.
Existing authentication, header allowlist, raw-body routing, closed admin surfaces,
response/status preservation and generic-proxy regressions remain in the full suite.

## Local mock memory measurements

Full gateway processes were measured separately from the mock upstream and client,
using `/proc/<pid>/status` RSS sampling and process high-water RSS. All calls targeted
local mock servers. Representative findings on this test machine:

- Startup idle approximately 55 MiB.
- One active plus three waiting near-32 MiB valid JSON requests: approximately
  216.3 MiB process high-water RSS. A further near-32 MiB request was rejected
  by the 96 MiB waiting-body reservation rule. About 152.2 MiB remained after
  one second idle, so memory is not claimed to return immediately to startup RSS.
- Valid 8 MiB and 128 MiB ZIP responses streamed with Content-Length upstream:
  approximately 55.8 / 55.7 MiB peak RSS, rather than response-size buffering.
- Small requests demonstrated one active plus four waiting; a sixth total request
  was rejected. Observed upstream concurrency stayed at one.

These are bounded mock experiments, not a production memory ceiling or a real-NAI
latency/availability guarantee. JSON object expansion, allocator retention, TLS,
request shapes and other services can change peak usage. The proposed 350–450 MB
service planning range is a target, not a promise. Keep the single-process profile;
multiple workers/replicas would have independent in-memory limits.

For deployment and timeout/error semantics, see [nai-utility.md](nai-utility.md).
