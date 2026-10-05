# WPE public-site canary — 2026-09-27

## Scope

The installed Cloud Browser service and its credentials/profile were not
changed. A separate unprivileged Pi checkout and temporary browser profiles
opened only `example.com` and `python.org`. Both engines ran headless without
the production egress proxy, so these numbers are **directional**, not a
production capacity or security comparison.

Three runs each opened one browser, then navigated to `example.com`,
`python.org`, and `example.com` again. The second run reversed engine order.
Each navigation was followed by a 4,000-character semantic observation.
Memory is the sum of child-process unique set sizes (USS), sampled after
startup and each page, not a continuous peak. The reusable test is
`scripts/engine_public_canary.py`.

| Measure (median of three runs) | WPE | Chromium |
|---|---:|---:|
| Browser startup | 1,151 ms | 1,889 ms |
| Median page navigate + observe | 742 ms | 1,275 ms |
| Highest sampled USS per run | 268.0 MiB | 363.4 MiB |
| Readable page observations | 9/9 | 9/9 |

WPE used about 26% less sampled USS and completed these page reads about 42%
faster by the medians above. Three short public pages are not enough to
generalize to authenticated or media-heavy browsing. Text extraction differed
between engines on Python.org; nonempty text was verified, not exact content
parity. Neither engine left a browser process after the benchmark exited.

Separate Pi smoke tests also passed one-tab reuse, observed-node click,
`example.com` screenshot, a public Python.org link click, a dummy login via
private RFB keyboard/mouse input, and pausing/resuming automation around the
protected screen. Dynamic Python.org screenshots sometimes failed closed with
`SCREEN_CHANGED`.

## Decision gates

**Do not promote this WPE candidate yet.** The Pi has Debian Trixie WPE WebKit
`2.48.3-1`; `apt-cache policy` showed no newer candidate in its configured
repositories. The [upstream August 2026 security advisory](https://wpewebkit.org/security/WSA-2026-0005.html)
lists several issues fixed only by 2.52.6, and the
[Debian security tracker](https://security-tracker.debian.org/tracker/source-package/wpewebkit)
marks Trixie's WPE package vulnerable with no security advisory update. A
supported, promptly updated WPE package/source is a prerequisite for browsing
untrusted sites.

The candidate also lacks verified network egress confinement against redirects,
DNS rebinding, and page subresources. Screenshot privacy checks are heuristic,
and a real authenticated-site login through the full private console was not
tested. These are independent blockers even after updating WPE.

The production control health endpoint returned `200` after the trials. The
test installed dependencies only into `/home/admin/cb-wpe-preview-20260927/venv`
and copied source only into `/home/admin/cb-engine-bench-20260927`. No
production files, secrets, or profiles were touched. No commit, push, or CI
publication was performed for this canary.
