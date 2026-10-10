# Client full compatibility — implementation and verification

## Boundaries

This work is on `feature/nai-client-full-compat`, not main. No production access, deployment, restart, real credentials or paid upstream generation was performed. Only synthetic credentials and loopback mock upstreams are used. Gateway code is prepared for delivery on the development branch only; exact pushed revisions and paired client/release status are recorded in workspace integration/STATUS.md. Gateway verification is not proof of deployed service or completed client UI acceptance.

## Compatibility / security design

Existing `configs/novelai-utility.yaml` remains the legacy single-entry configuration. Merely updating source does not enable native routes. `server.nai_utility.native_enabled: true` is an explicit opt-in and requires `enabled: true`, authenticated server keys, `apis.novelai` keys and fixed `upstreams.image/account/text` origins. Native mode exposes only exact method/path business pairs plus health; config/dashboard/info/metrics/docs and arbitrary `/user/*` are not exposed. No redirect following, client-controlled origin, dynamic URL, browser session auth or inheritance of client cookies/default identity headers.

The HTTP executor is shared with NyaProxy; the compatibility module does not introduce another proxy framework. Native paid operations deliberately bypass the generic rotating/retrying queue: deterministic SHA-256 modulo the configured upstream key list binds the canonical authenticated downstream credential (Bearer token whitespace normalized identically to auth) to the same upstream for account and generation. Reordering/changing that list changes bindings and requires clients to invalidate refreshed account state. This is a shared upstream wallet, not separate balances per proxy key. No account cache is kept.

A bounded admission object per upstream key reserves downstream upload/body memory before consuming input. Bounded downstream body ingestion can occur concurrently (at most two upload readers per binding, also bounded by admitted request count and reserved memory); these readers do not contact the upstream. After body ingestion, a one-operation semaphore is acquired before dispatch and covers the complete upstream request-body upload, generation/execution and entire upstream response streaming lifecycle. It is not held during downstream ingestion or queue waiting. Account reads have independent concurrency and per-binding/path frequency bounds. Queue fullness is immediate 503; timeout and disconnect cancel queued dispatch and response finalizers close transport before returning capacity. JSON bodies are object-validated; multipart bodies preserve their content-type boundary and opaque bytes without parser rewriting. Business bytes are not rewritten; stream framing is opaque raw-byte pass-through, not translated to SSE. Compression headers and raw compressed bytes stay paired. Response identity/cookie/private headers are suppressed in native mode.

Account gzip is incrementally decoded and validated before JSON parsing, with `max_account_response_bytes` applied independently to both compressed wire bytes and decoded bytes (default 65536 each). Truncated/invalid gzip, trailing members and decompression overflow fail closed with 502; no unbounded `gzip.decompress` is used. Account responses are bounded and projected only to a hard allowlist of evidence-backed scalar leaves. Endpoint-specific defaults preserve subscription tier/active/expiresAt, numeric or structured trainingStepsLeft, usage isNegative/percent/timeUntilNextPercent, and trial/ban fields. `/user/data` retains the subscription wrapper; `/user/subscription` is a direct subscription object. Optional `account_fields` narrows these fields, never broadens them. Missing/null fields stay omitted (unknown); zero and false retain their original meaning. No keystore/settings/email/marketing/credentials or full personal-data objects are returned. Invalid upstream JSON is 502, invalid downstream input is 400. Public official evidence is from the APP endpoint table and Swagger, recorded in workspace `integration/evidence/official-protocol.md`; it is static evidence, not paid live testing.

## Actual evidence in this worktree

Windows CPython 3.12.13 via repository-local uv environment:

- Initial unit run: **366 passed, 1 failed**. Failure was existing POSIX-only `chmod` mode assertion on Windows (0666 vs 0600). Test now explicitly skips that POSIX assertion on Windows; Windows ACL protection is **not verified**.
- New subprocess/real localhost HTTP native suite: **15 passed in 26.88s** (before subsequent added unit tests/read-lifetime improvements).
- First full suite: **443 passed, 4 failed, 1 skipped in 339.74s**. Existing e2e subprocess stdout/stderr used undrained pipes; expected upstream tracebacks/burst logs filled Windows pipe buffers and blocked the server event loop. Replaced those pipes with temporary log files. The 11 affected failure/load scenarios then passed in 73.66s.
- Full suite after harness fix: **447 passed, 1 skipped in 194.69s**.
- Full suite after official-evidence multipart/account/default-field changes: **448 passed, 1 skipped in 198.99s**. This includes all unit and localhost subprocess e2e tests; one existing Starlette HTTPX deprecation warning. This was before the final error-classification/log/deadline hardening below.
- **Pre-independent-review full run (superseded)** (unit then real localhost HTTP e2e): `uv run --extra dev python -m pytest tests/unit tests/e2e -q --tb=short` — **449 passed, 1 skipped, 1 warning in 198.75s**. Includes the final invalid-account 502 test, per-business stream deadline, suppressed utility header debug logs and environment isolation.
- Independent review reproduced three defects not covered by that 449 run: canonical Bearer identity mismatch, header-wait execution deadline, and compressed account JSON decoding. All three now have code fixes and new real localhost HTTP regressions; targeted native suite **22 passed in 40.64s**. **Post-independent-review current-source full suite: 454 passed, 1 skipped, 1 warning in 203.95s**, using `uv run --extra dev python -m pytest tests/unit tests/e2e -q --tb=short`. The earlier 449 count is not post-fix acceptance evidence.
- Independent read-only review re-executed the patched full suite: **454 passed, 1 skipped, 1 warning in 203.02s** (`pytest tests/unit tests/e2e -q --tb=short -p no:cacheprovider --no-cov`), plus six focused real localhost regressions for the three defects. Parent separately re-executed native unit+E2E: **29 passed in 35.97s**. These are current-patch local proofs, not live official verification.
- Final quality checks: `ruff check nya tests` **All checks passed**; `ruff format --check nya tests` **78 files already formatted**; configured mypy ratchet **no issues in 14 source files**; `git diff --check` clean. Mypy is the repository ratchet, not whole-codebase type proof.

Native HTTP coverage includes multipart boundary/raw-byte preservation, safe default account projection (numeric/structured training steps, missing usage, percent=0 vs isNegative=true), invalid upstream account JSON returning 502, queued client cancellation with no dispatch and queue-full 503, each declared method/path, unknown paths/suffixes/trailing slash/wrong method, invalid key, no admin surface, query-host rejection, original request bytes, upstream-only credential, identity header stripping, account/generation binding, account projection, raw synthetic MessagePack-like framing, incremental streaming, gzip pair preservation, 402 byte preservation, 429 without generic retry, read independence during generation stream, timeout without redispatch and capacity recovery. The frame bytes are synthetic transport fixtures, **not official protocol evidence**. Existing admission tests separately exercise upload bounds, cancelled waiters, full queue and stream lifetimes.

### Reproduce locally

Run from the repository with `REASONING=medium`:

```powershell
uv run --extra dev python -m pytest tests/unit tests/e2e -q
uv run --extra dev --extra lint ruff check nya tests
uv run --extra dev --extra lint ruff format --check nya tests
uv run --extra dev --extra lint mypy
```

Use only temporary loopback config for mock launches:

```powershell
.venv/Scripts/python.exe -m nya --config TEMP.yaml --host 127.0.0.1 --port PORT --no-reload
```

The E2E fixtures clear remote/config/server environment overrides and use synthetic temporary configs. An additional unit-before-e2e run exposed an existing launcher unit test's direct environment mutation (dummy `remote.test` inherited by child processes), causing setup failures rather than business requests. The harness now isolates those variables explicitly regardless of host/unit ordering. No real service was accessed by those fixtures.

## Migration and rollback

1. Back up the actual configuration securely; do not copy repository example over deployed ports/TLS/firewall/queue settings. Actual deployed queue limits remain unknown.
2. Keep `server.api_key`, `apis.novelai.variables` and effective queue/body/upload policy from that installation. Configure fixed verified upstream origins separately for image/account/text. Native never rotates keys after an uncertain paid outcome.
3. Opt in with `native_enabled: true`; retain `legacy_entrypoint: true` for ordinary/legacy SSE/Vibe classifier compatibility. Set false only for clients that use all exact native routes (including ordinary generation with arbitrary valid business JSON).
4. Configure `queue_wait_seconds`, `generation_timeout_seconds`, `upscale_timeout_seconds`, `text_timeout_seconds`, `account_timeout_seconds`. Coordinate client deadlines: waiting 120, generation 300, upscale 300, text 120, read-only 15 seconds. Native paid end-to-end admission deadline is queue wait plus business timeout. Dispatch creates a separate absolute business deadline: it bounds the entire wait for response headers as well as subsequent streaming; socket timeouts additionally use that business budget. Queue budget cannot extend execution. Read-only uses its independent 15-second budget. A deadline after upstream dispatch does not prove generation was unpaid; no blind retry.
5. Leave `account_fields` omitted for verified endpoint-specific defaults, or narrow it. Refresh account state whenever origin or binding key configuration changes. A missing field means unknown.
6. Verify against a local recording mock first. Real authenticated checks/paid validation need separate explicit authorization.
7. Roll back by restoring the saved configuration and previous source/package through the normal authorized deployment procedure. To retain new source but return to single-entry behavior, remove native options or set `native_enabled: false` and keep `only_entrypoint: true`. Old full generate-image URL is unchanged.

## Unverified / pending

Official live authenticated calls, production TLS/deployment and paid outcome semantics have not been tested here. The optional suggest-tags query whitelist (`model`, `prompt`, `type`, `lang`) is now statically verified in official-protocol evidence section 8, but this optional gateway route is intentionally not implemented/exposed or tested in this change; existing local CSV completion is retained. `/oa/v1/models` remains unverified and unexposed. Image/account origins are `https://image.novelai.net`, text is `https://text.novelai.net`, verified from the current public official APP endpoint table. UI/client framing is owned by the client implementation. No claims of deployment or real service verification are made.
