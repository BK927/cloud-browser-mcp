# Public reading / bounded navigation validation — 2026-10-05

This report concerns `codex/public-read-stability`, based on main `9b514f6`.
The separate unfinished WPE checkout and production installation were not changed.
DrissionPage remains 4.1.1.4. There is no engine switch, RAM/swap increase,
credential reset, profile conversion or route change.

## Local environment and scope

Windows, Python 3.13.11, Chrome 154 and disposable headless development profiles.
Controlled browser tests use loopback fixtures; the public canary uses fixed public
DCInside/YouTube routes. No real account login, site write or CAPTCHA bypass was
performed. These are **not** Pi/1GiB measurements or web ChatGPT verification.
Temporary test files and detailed evidence are outside OneDrive, on a dedicated
E-drive directory. Earlier C-drive disk-full failures are retained, not counted
as passing tests; the user's freed disk space did not authorize deleting user files.

## Regression evidence

- The final general suite passed **615 tests / skipped 5** (91.71 seconds).
  The separate current client-progress/memory-probe suite passed 20.
  Overlapping tests are not added as unique coverage.
- The broad Chromium run passed 177 / skipped 4, with one outdated HTTP-test
  expectation failing because cold startup correctly returned pending, not `ok`.
  The harness was updated to consume owned progress rather than changing the
  production response or extending the initial five-second wait. Its final
  HTTP/client/probe suite passed 21, including real worker/Chromium image delivery.
- After the frame-handle and lifetime-monitor repairs, frame/capture/extension/
  private-console regression passed 57 / skipped 1. Native WebMCP registration
  remains unavailable in this local Chromium. Static private frame masks,
  cross-origin and nested frames, transient password/OTP/Shadow DOM changes,
  retry-gap protection and bounded presentation-only recapture were exercised.
- Two actual history-roundtrip capture cases passed. Both restored the **same
  loader** (`restored_same_loader=true`), exercising Chromium document restoration;
  neither the auto nor visual path returned the private screenshot. SDK callbacks
  remain chained, and private-control entry removes our monitors.
- Three opt-in public tests passed: W3C accordion state change and real HTTP MCP
  worker/image plus strict/balanced approval paths. This proves local MCP transport,
  not ChatGPT's integration or a user's real login.
- Ruff and whitespace checks passed. Dependency-level warnings were retained:
  Starlette's deprecated AnyIO alias, pytest's JUnit property-format warning,
  and one DrissionPage thread warning after a controlled frame detachment. The
  latter is not a claim of warning-free engine behavior or verified process exit.

### Actual navigation longer than the worker watchdog

A controlled page has a 48-second secondary resource. A one-second budget returned
`NAVIGATION_TIMEOUT`; the 60-second request completed after **49.051 seconds**.
The worker PID stayed unchanged. During that navigation:

| Owned status | Other work's fresh text | Other work's cleanup |
|---:|---:|---:|
| 0.004336 s | 0.077916 s | 1.227625 s |

Chromium/IPC were real; resource admission in this fixture was deterministic,
not a Pi memory-pressure benchmark. Five-minute boundaries, budget precedence,
duplicate requests, HTTP cancellation, delayed progress probes, idle TTL,
private-control cancellation and scoped browser exit were also tested.

## Three fixed-public-page repeats

The final canary deliberately returned **exit 1 / `ok=false`**, since refused
images count as failures. No request was repeated until it succeeded.
Allowlisted per-attempt numerical data are in
[benchmarks/public-read-2026-10-05.json](benchmarks/public-read-2026-10-05.json).

| Requested result | Success |
|---|---:|
| Four fixed pages: navigation / semantic / auto | 12/12 each |
| DC fixed article's scoped body | 3/3, 287 characters each |
| Returned DC link → same article + rendered body | 3/3, 284 characters each |
| Public numeric DC `no` links preserved | 39/39 sampled links |
| YouTube scoped description | 3/3, 138 characters each |
| DC list image | 3/3, first attempt |
| DC article image | 1/3, first attempt |
| YouTube search and video images | 6/6, first attempt |
| Run-owned temporary directory cleanup | 3/3 |

The two DC article image refusals were `SCREEN_CHANGED` with `frame_document`,
one attempt each. A document/attachment lifetime change does **not** establish
that visible secret pixels or a bot block occurred; the fixed diagnostics do not
contain enough content to make that attribution. It does establish that the
pre-capture proof cannot be reused, so automatic retry was correctly withheld.
Text remained available. Coarse lifetime checks can still refuse an image during
unrelated embedded-document churn; reducing that further requires bounded,
visibility-aware evidence, not ignoring document changes.

YouTube has two `#description` matches: an ancestor-hidden zero-sized first match
and a rendered second match. Rendered scoped selection read the latter. Partial
frame limitations remain explicit; no missing frame was reported as fully read.
Browser shutdown returned and temporary directories were removed, but all browser
subprocess exits were not independently proven by this canary.

The detailed local sanitized canary artifact is `public-read-publication.json`,
SHA-256 `c428d27e097a7521111d034e2ae587d975faacc975d9ebca57a534bfb31d2518`.
It contains no scraped body, image bytes, credentials or raw engine error text.

## Publication and operational gates

Exact-commit CI is recorded by GitHub Actions on the published branch; this local
report is not itself proof of Docker/native runtime or multiarchitecture success.
No local Docker runtime is installed. Require contract/Chromium tests, passkey
regression, Docker runtime, native runtime and arm64/amd64 build for the exact
candidate before operational application.

The existing **배포용 세션** alone owns Pi installation and comparison. It must first
verify the exact installed image/source, real idle state, backups and rollback
identity under directly authenticated administrator access. Keep current Docker,
1GiB RAM, unchanged swap, 1024×768 and other MCPs. Explicitly apply the agreed
60-second default and `inspect` frame policy only after the privacy gate; preserve
all other environment values, OAuth DB, passkey setup and profiles.

Compare old/new three times each, retaining failures and recording response time,
CPU, PSS, cgroup memory/cache/swap, OOM and post-close recovery. Use the same
benchmark script **and** byte-identical `client_progress.py` helper beside it for
old baseline compatibility. `initial_response_ms` is not load completion time;
`elapsed_ms` includes owned progress through the final result.

Pi savings and the final user-authenticated web ChatGPT schema/timeout/progress/
image path remain **unverified**. Actual accounts, playback and subtitles are
outside this repair. Passing local tests does not complete these operational gates.
