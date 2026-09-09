# Browser reliability and dual installation worklog

Baseline: `b7bd34b97d7391a1c691985c8e7714b8d493262e` (2026-09-08).
The baseline deployment and its prefix rollback remain owned by the existing
deployment task. No credentials, profiles, routes or sibling services are to be
replaced. Native installation is an additional path, not permission for a
permanent migration. Project code remains MIT; engine licensing is unchanged.

## 2026-09-10: replace conversation-long exclusivity

The original exclusive-work decision below is superseded by isolated concurrent
work leases (default ceiling two), with one bounded FIFO browser command at a
time. Profiles and managed displays are separate; files require an explicit
destination work. Manual control does not cancel another work's downloads.
Global status now separates executing, work capacity, cleanup and human control,
and states whether owned commands may queue. Idle work is renewed by processed
operations, not status polls, and a background reaper verifies expired closure.
Human completion remains possible after work TTL expiry without auto-unlocking
an unfinished credential screen.

Adaptive admission distinguishes startup, navigation and capture costs; clean
cache accounting and host/cgroup hard bounds remain. PSI stalls can deny new
admitted work. The API/browser failure domain is unchanged. No unlimited memory,
live-tab eviction, permanent native migration or public credential changes are
part of this patch. Capacity/control logs are bounded and payload-free, with
MCP request-ID correlation; outer connector error labels remain client-owned.

Validation adds two-work privacy/ownership, idle cleanup, queued timeout without
dispatch, manual expiry recovery, file ownership, PSI and real independent
Chromium profiles. The Docker/native disposable runtime probe also requires
two distinct Xvfb processes and verifies that the private viewer targets the
requested work after another work has opened. Target deployment remains gated
on exact-commit CI and a user-approved restart window; implementation tests are
not evidence that the Pi is already running these bytes.

Integration found that Ubuntu/Google Chrome can refuse a second work for real
memory pressure at 1GiB. A subsequent run admitted it but Chrome reported
`pthread_create: Resource temporarily unavailable` under the old 256-task
service ceiling. Both installation paths now have a finite operator-owned 512
default task ceiling, independently of unchanged RAM/swap budgets. Native CI
records exact-service PSS/cgroup/task counters before teardown; this sample
excludes the already-exited MCP test client and is not a peak or Pi measurement.
Native CI separately exercises (a) 1GiB lifecycle/recovery, accepting only a
measured pre-allocation RESOURCE_PRESSURE denial that leaves the first work
intact, and (b) a mandatory two-work/control lifecycle at 2GiB on the disposable
Ubuntu runner. Docker's two-work gate remains at 1GiB. Neither native CI result
proves two profiles always fit at 1GiB or changes the Pi's deployment budget.

## Accepted implementation sequence

1. Exclusive authenticated work leases; cancellation-safe dispatch and result
   retrieval; target-scoped validity; bounded observation and memory recovery.
2. Balanced-v2 ordinary editing; frames and privacy-safe screenshots.
3. Expanded mouse/keyboard, scoped observation, waits, dialogs, diagnostics,
   isolated artifacts/clipboard, operator-authorized read-only page tools.
4. Shared Docker/native runtime with on-demand display/control services;
   Debian 13 arm64/amd64 installation, isolation and resource controls;
   regression/CI and three-way Pi comparison.

Each stage needs focused regression tests and a public commit with exact-commit
CI. Real Pi measurements are separate evidence: old Docker, improved Docker,
and improved native, three repeats each, identical 1 GiB budget and workloads.
Do not report unrun installation, isolation or performance tests as passing.

## Progress

- Stage 1 implemented: authenticated exclusive leases, private administrator
  reclaim, cancellation-shielded dispatch/result retrieval, cached non-blocking
  status, bounded command queue, target-preserving node IDs, own-form/input
  digests, explicit last-tab termination, lightweight pressure observations,
  bounded/cached AX work, process/ancestor cgroup accounting.
- Local regression: 368 passed / 4 skipped / 1 upstream warning (234.34 s);
  subsequent final-change focused tests: 46 passed (24.16 s). Full CI runs the
  committed source independently. Public-site live tests were not enabled in
  this run; optional native WebMCP/default runtime remains separately skipped.
- Stage 1 public commit: `5ae42537809dc4e432f3df3072a1a202273a3742`.
  CI run 34238001679 passed on the second attempt. First attempt: 315 non-browser
  tests passed; 55 browser tests passed / 4 skipped / one standalone OAuth Chrome
  fixture startup error (`BrowserConnectError`). No action retry or sandbox
  weakening was added; the failed-run evidence remains available.
- Stage 2 implemented: balanced-v2 ordinary editing/search, bounded CDP frame
  inventory and exact-node actions (same/cross-origin, nested and legacy frames),
  selective sensitive-frame masking, dynamic imagery and semantic coordinate
  validation. Focused regression: 96 passed / 1 skipped; additional frame tests:
  6 passed (25.52 s), including nested occlusion and changing canvas pixels.
  Broad local run: 378 passed / 4 skipped; one outdated fault-injection helper
  rejected the new capture keyword arguments. Updated the helper signature and
  re-ran the complete completion/error suite: 12 passed (48.77 s). The production
  post-dispatch uncertainty behavior and assertion remain unchanged.
- Stage 2 public commit: `d929d1a04251b2fa4c3d8657bcfa99d44e1e2df2`;
  exact-commit CI run 34241565720 passed tests and arm64/amd64 build.
- Stage 3 implemented: scoped observation, sequential/modifier/mouse/drag/multi-
  select inputs, bounded waits/follow-ups, explicit dialogs, metadata-only logs,
  work-local clipboard, downloads/exports/private attachment retrieval, intercepted
  file chooser nodes, operator-pinned WebMCP reads, public URL query/anchors.
  Full local regression: 389 passed / 4 skipped / 1 upstream warning (271.36 s).
  Binary download disclosure stays private; console arguments/exception locals
  are intentionally withheld. Native HTML5 drag-data behavior is not guaranteed.
  Final edge suite: 16 passed (39.41 s), including exact hidden-file chooser upload,
  operator-pinned WebMCP allowlist, protected dialog rejection and input release.
- Stage 3 public commit: `a9b37688ee59a4098551deaf418bb18dab437e9a`;
  exact-commit CI run 34244506193 passed tests and arm64/amd64 build.
- Stage 4 implemented: shared on-demand Xvfb/Chromium/VNC lifetime, protected
  Xauthority, cross-installation data lock, fixed cgroup-scoped crash cleanup,
  Debian native installer/update/status/uninstall, dedicated users/netns/egress,
  protected isolation attestation and exact systemd memory-budget startup gate.
  Operator environment/customizations and old releases are preserved. Native
  files do not stop/remove Docker or other MCPs. Added actual Docker/native CI
  runtime probes and same-cgroup benchmark/PSS sampling tools. CI/Pi execution
  results for this stage are pending, not inferred from static checks.
  Local final regression: 402 passed / 4 skipped / 1 upstream warning (284.34 s).
  Focused native/control safety checks: 15 passed after fixing two test-only
  platform/assertion-placement mistakes; production assertions were not relaxed.
- At this stage-4 checkpoint, remaining: exact regression/CI and owner Pi comparison
  (completed later; see the final comparison section below).
  No native performance savings have been established. No operational deployment
  has been requested for this intermediate source.

## Stage 4 integration findings

- Actual local public-site checks: W3C accordion direct adapter plus strict and
  balanced HTTP MCP/worker/private-approval paths, Python documentation reading
  and 1024x768 image decoding: 3 passed (24.95 s).
- `ef325bb`: Docker probe found that docker exec does not inherit entrypoint
  exports. Fixed benchmark to read the exact same-UID/cgroup running API's
  environment without logging it; the standalone script also supports baseline.
- `f698aa3` and `c1383a3`: Docker runtime passed. Native startup diagnostics proved
  the browser HOME and profile writable but an inherited `/home/runner/.config`
  XDG setting was inaccessible under ProtectHome. A dedicated HOME/-H and profile
  group umask are retained; the decisive fix clears desktop XDG/Chrome overrides
  in a root-owned engine wrapper **after sudo/PAM**, shared by both installers.
- `fe084f7`: actual native runtime job passed, including 1GiB cgroup, authenticated
  MCP image, shared on-demand display/control, no-new-privileges/seccomp child,
  worker SIGKILL cleanup/reopen, browser UID control-plane denial and proxy denial
  of private destinations. These Ubuntu/Chrome CI results are not Pi measurements.
- Final recovery checks add a caller-owned lease even for a partial failed open,
  prevent work transfer after failed expiry cleanup, keep cached human-control
  status available after work expiry, and sweep only expired generated orphan
  artifacts. Focused recovery suite: 34 passed (2.70 s).

## Pi native-install permission finding (2026-09-09)

- Deployment-owner comparison of `9f5a21f` completed one baseline Docker run and
  one improved Docker run, but native startup failed before measurement. This is
  not a completed nine-run comparison and proves no native memory savings.
- Evidence: the conservative controller umask `0077` left `/opt/cloud-browser`,
  its release/venv ancestry, and `/etc/cloud-browser` at root-only `0700`.
  systemd reported `200/CHDIR` and `203/EXEC`. `browser.env` itself correctly
  retained `root:cb-api 0640`; its parent was the blocking boundary.
- The deployment owner restored the original containers, images, environment,
  routes and healthy OAuth connection; other MCPs remained intact. Failed native
  installation and partial measurement evidence were preserved, not chmod'd in
  place to bypass the installer defect.
- Repair explicitly creates protected public code/config directories, confines
  `0022` to dependency creation (restoring the caller umask even on failure),
  avoids importing permissions through an old wheel cache, and checks imports
  as all four service users before stopping an existing native service. It does
  not recursively relax previous releases, credentials, database or profiles.
- Regression coverage adds POSIX mode/symlink/failure tests and real CI native
  install plus same-release update under `0077`, with negative credential access
  checks. Exact repair-commit CI and a fresh Pi comparison remain required.
- First repair CI (`9fe97cf`, run 34258921778) passed the POSIX unit tests, but
  the installer rejected the CI-created diagnostic prefix as unprotected. The
  disposable fixture now explicitly reproduces the reported root-only `0700`
  prefix without inheriting hosted-runner ACLs; production rejection of a
  foreign-owned or group/other-writable existing directory remains unchanged.

## Pi native network-manager finding (2026-09-09)

- `0cb6208` passed all four CI jobs (34259260581): 410 tests passed, four optional
  browser checks skipped. The Pi then passed its installer/import and public/
  private permission gates; the earlier failed venv was preserved unchanged.
- The second Pi comparison again completed only one baseline and one improved
  Docker run. Native failed before measurement: egress could not bind the exact
  configured `10.203.87.1:3128`. The original deployment was restored and siblings
  checked healthy. No native memory-saving result exists yet.
- Safe timeline shows NM changed the new `cb-host0` from unmanaged to unavailable
  to disconnected immediately before egress startup. NM was active, networkd/
  dhcpcd inactive; egress ran in the intended host namespace. There is no failed-
  moment IP snapshot, so actual IP removal by NM is an inference, not a proven
  event. The observed overlapping device management is independently actionable.
- Repair relinquishes only the newly created veth pair from NM at runtime before
  address assignment, verifies IP/link/manager readiness and egress namespace,
  and separates readiness from identity/firewall-verified teardown. It does not
  alter host network-manager profiles, reload NM, change other MCPs or widen bind.
- Added bounded discovery/denial/IP-loss/order regression and an actual NM test
  in a network-none disposable container. Exact repair CI and a fresh, short Pi
  native startup gate are required before another full nine-run comparison.

## Final Pi comparison and restoration (2026-09-09)

- Measured source: `3a172a12334378e958f4c84d4d944436b24d6442`.
  [Exact-commit CI 34266596406](https://github.com/BK927/cloud-browser-mcp/actions/runs/34266596406)
  passed tests, Docker runtime, native runtime with actual isolated NM, and
  arm64/amd64 build. Linux: 513 passed / 4 skipped; local: 509 passed / 8 skipped.
- Deployment owner passed the fresh Debian 13 arm64 installer/import/network/
  authenticated-status gate, then measured baseline Docker, improved Docker and
  improved native three times each at 1GiB RAM + 1GiB swap. All nine benchmark
  processes exited 0 and closed their sessions. Earlier partial sets are excluded.
- Idle three-role PSS medians: 256.40 / 190.85 / 179.34MiB. Native saved a further
  11.51MiB idle and 6.04MiB on the single page relative to improved Docker. The
  Docker daemon remained active; this is not pure container overhead measurement.
- RESOURCE_PRESSURE counts were 12 / 3 / 3. All variants refused all three long
  full-page captures; exit 0 does not mean every request succeeded. No oom or
  oom_kill increments; native run 1 recorded ten browser memory.events:max events.
- Original production baseline, credentials, profiles, routes and sibling services
  were verified restored at 04:46:05 KST. Test containers and four native units are
  stopped; dedicated test namespace removed. Recovery copies and failed evidence
  retained. No permanent upgrade or native migration was performed.
- [Measured report](PI_COMPARISON_2026-09-09.md) and
  [allowlisted numeric data](benchmarks/pi-2026-09-09.json) record all nine runs,
  sampling limits, source identities and remaining user-environment gates. New
  web ChatGPT image understanding and Debian amd64 field deployment are not
  inferred from Pi/CI results. These documentation changes are not a new release.
