# Browser reliability and dual installation worklog

Baseline: `b7bd34b97d7391a1c691985c8e7714b8d493262e` (2026-09-08).
The baseline deployment and its prefix rollback remain owned by the existing
deployment task. No credentials, profiles, routes or sibling services are to be
replaced. Native installation is an additional path, not permission for a
permanent migration. Project code remains MIT; engine licensing is unchanged.

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
- Remaining: stage 2 frames/images and balanced-v2; stage 3 expanded operations;
  stage 4 native/on-demand runtime and deployment-owner Pi comparison.
  No native performance savings have been established. No operational deployment
  has been requested for this intermediate source.
