# Expanded operations (contract 0.4 draft)

## Authentication lifetimes

OAuth access tokens last 15 minutes by default. A refresh token and its grant
now renew together when ChatGPT refreshes a live connection: the idle lifetime
is 30 days (`CB_REFRESH_TTL=2592000`), with an absolute 90-day limit from the
grant's creation (`CB_GRANT_MAX_TTL=7776000`). A revoked or expired grant cannot
be renewed. Existing live grants are migrated on their next refresh; a grant
that already expired still requires the normal authorization flow. Changing
these settings does not rotate credentials or clear the authentication store.

The private operator console has a separate eight-hour login cookie
(`CB_CONTROL_SESSION_TTL=28800`). This is not the browser work lifetime:
`CB_SESSION_TTL=3600` still expires idle browser work after one hour.

All session calls require the authenticated work's `lease_id`. The MCP tool
schema is authoritative for input bounds. These additions use the existing
worker, node validation, human-control lock and approval/execution journal.
There is no arbitrary JavaScript evaluation tool.

## Work scheduling and adaptive memory

`CB_MAX_SESSIONS=2` is the default work-capacity ceiling, not a fixed tab ceiling.
Each work owns a separate Chromium profile and managed X display. Browser IPC
is serialized per command in a bounded FIFO queue; waiting for the next user
message does not reserve the execution slot. An operator can retain single-work
mode with `CB_MAX_SESSIONS=1`. Increasing this ceiling does not bypass memory
admission, and background pages still consume RAM/CPU. Two heavy sites are not
guaranteed to fit a 1GiB browser budget.

`CB_MEMORY_POLICY=adaptive` can borrow the normal 256MiB soft reserve down to
`CB_MEMORY_FLOOR_MB=96`, after allowing for the requested operation's estimated
cost. The host must still retain the normal reserve plus that cost. Starting
a profile budgets 192MiB; a new tab 96MiB; navigation 64MiB. Capture estimates
32MiB plus 16 bytes per pixel, checked in the worker immediately before capture
using the actual viewport/full-page geometry. These are admission estimates, not per-operation hard caps or
measured guarantees. `strict` retains the normal reserve for every admitted task.
PSI's worst host/cgroup avg10 is reported; full stalls >=10% or some stalls >=50%
deny new admitted work. PSI absence is reported as unknown, never zero.
Small fresh text observations and explicit cleanup remain available under
pressure. Auto observations retain bounded fresh main text plus controls; a denied
optional capture is reported in `observation.screenshot_omitted`. An explicit
visual request returns RESOURCE_PRESSURE rather than pretending to return an image.
`observation_revision` identifies the text snapshot. If capture advances the
page revision, a truncated continuation is invalidated (`pagination_stale=true`)
and the client must observe again, rather than consume a stale cursor.

Neither policy changes `memory.max`, swap limits, the network sandbox, approval
rules or another work's tabs. There is no AI command that removes hard limits.
The API and browser still share a cgroup/worker failure domain; this change does
not promise independent process-crash recovery or host immunity from swap pressure.
Native and Docker use this same policy and retain their operator RAM/swap limits.
The finite process/thread ceiling is separate: new installations default to 512
(`CB_BROWSER_TASK_LIMIT` in Compose; native `--tasks-max`, preserved on update).
The old 256-task ceiling can prevent a second Chromium from creating threads.
Changing this ceiling does not allocate RAM or raise `memory.max`; the actual
threads still consume memory within that unchanged budget. Native startup also
verifies its installed task limit. It is not an AI-configurable setting.

Idle TTL is renewed by processed work, not by status polling. Expired work is
reaped periodically without clicking/observing it. Human control remains protected
until explicit completion/cancellation, including after idle expiry. Private file
staging must select the destination work when more than one exists. The console's
reclaim action closes only the selected work.

Capacity/control failures include a domain `error.code`, category and a safe
reason where applicable. Up to 120 payload-free capacity log records per minute
contain the exact MCP `request_id`; excess records are dropped. No URL, session,
lease, arguments, page text or credentials are logged. ChatGPT/connector wrappers
may still label an MCP error `INVALID_ARGUMENT`: the server cannot control that
outer label and does not disguise failed calls as success to suppress it.

## Observation and completion

### Bounded asynchronous navigation

`browser_open.timeout_ms` and `browser_navigate.timeout_ms` override the tab's
`browser_configure.configuration.navigation_timeout_ms`, then
`CB_NAVIGATION_TIMEOUT` (seconds; new-install default 60). The immutable operator
ceiling is `CB_NAVIGATION_MAX_TIMEOUT` (default 300 seconds). Values outside
1000..ceiling milliseconds are refused before browser execution. Updates do not
rewrite an existing environment; apply 60 seconds explicitly through the deployment owner.

Open/navigation wait at most five seconds for their initial MCP result. A
`no_change` response with `navigation.pending=true` is **not a completed load**.
Retain its `session_id`, `lease_id` and server `operation_id`; query
`browser_status` with those identifiers. Slow browser initialization may also
return `tab_id=null`; take the actual tab from the completed operation result.
The first response being lost cannot be recovered from a client-chosen name:
use the private console to reclaim that work.

Status exposes owned phase, elapsed time, applied timeout and saved final result,
without waiting for the command lock. Readiness probes run at most twice per
second, with that lock released between probes. Only one navigation per work
can be pending. Further navigation or action on that tab returns
`NAVIGATION_IN_PROGRESS`; another work can use its own admitted commands.
Use the same optional `browser_navigate.operation_id` only with identical
arguments to replay/query an already dispatched request. Different arguments
return `OPERATION_CONFLICT`. The bounded result journal does not survive restart.

Completion requires the requested document/history transition and
`document.readyState=complete`, not just readable body text. A timeout is
`NAVIGATION_TIMEOUT` with a phase (`command_response`, `document_transition`,
`loading`, `final_verification`) and independently confirmed `current_page`
where available. A preflight timeout has `dispatched=false`. Neither a late CDP
reply nor an HTTP disconnect causes retransmission. The 45-second IPC watchdog
remains a worker-failure bound: no production command waits for the entire load.

Closing the exact tab/work, or entering its private control, cancels its pending
navigation. Private control suppresses all background DOM/image probes. A
confirmed browser-process exit expires only its work and releases its display;
a dead/corrupt IPC worker, or a disconnected browser whose exit cannot be
verified safely, invalidates all works in that worker failure domain.

### Capture consistency and limited recapture

Image proofs classify `SCREEN_CHANGED` using fixed `capture_reasons`: `document`,
`viewport`, `scroll`, `frame_document`, `public_frame_geometry`,
`protected_geometry`, `privacy_history`, `privacy_unbounded`. They contain no
page content. Only independently verified presentation changes (viewport,
scroll or ordinary-frame position) permit one fresh observation and recapture
after 200ms. Two attempts share a 15-second processing budget; each attempt
checks actual capture size and memory again. An uninspected/new frame cannot
reuse another frame's privacy proof.

Document/frame replacement, privacy history changes (including appearance then
removal of a password/OTP field), auth, uncertain masking, memory refusal and
`CAPTURE_TIMEOUT` never trigger automatic recapture. Changes during the 200ms
gap are also checked. The successful image's new `screenshot_id` and
`capture_attempts` refer to the actual capture, not an older image. Failure
keeps fresh auto text and records the image omission; explicit visual requests
still fail honestly. This is not a promise of universal site or image success.

`browser_observe.query` accepts `frame_id`, CSS `scope` and `selector`, `role`,
`name`, `label`, and `limit` (1–100). Scope/selector only locate observed DOM
objects; subsequent actions require the returned backend-bound node IDs.
Name/label filters are normalized, case-insensitive substring filters; role
requires an exact normalized match. Only visible viewport nodes are actionable.
Semantic CSS queries read rendered matching subtrees within bounded text/scan
budgets, not the first hidden duplicate. The query limit bounds issued nodes,
not the entire semantic text.
`query_match_count` and `query_empty_reason` explain missing, hidden, protected,
empty and scan-budget-limited results. Query limits and partial frames are
reported, not represented as a complete DOM dump. Omitting query on a new
observation resets its scope; a cursor retains its original snapshot.

`browser_wait` accepts a condition with type `url` (exact `value`), `element`
(`query`, state present/absent/visible/hidden/enabled), `dialog`, or `download`
(completed). Waits are bounded to 10 seconds; timeout is `no_change` with
`wait.timed_out=true`. Present/absent tests DOM existence, including hidden
elements; visible/hidden tests rendering, including offscreen elements.
Incomplete queries do not establish absence or hidden state.
Use narrow selectors; a partial observation is not a global absence proof.

`browser_act.completion` uses the same condition and optional
`completion_timeout_ms`. `follow_up=true` requests at most 2,000 characters of
interactive observation. A failed completion wait does not erase the fact that
the action was dispatched. Never repeat a submitted action because its wait
timed out. Poll `browser_status(operation_id=...)` after a lost HTTP response.
Known completion/target-state mismatch is `ACTION_GOAL_NOT_MET`, not `ok`.
An incomplete or unavailable post-action observation is `RESULT_UNCERTAIN`.
`fill`, `select`, `select_multiple`, and `check` report `target_state_verified`;
`performed` separately records whether the action was delivered.

## Keyboard and mouse

Added action types: `type` (append sequential Unicode text), `right_click`,
`middle_click`, `drag` (source `node_id`, destination `target_node_id`), and
`select_multiple` (`values`). Typing's total artificial delay is at most 8 s;
drag uses 2–30 bounded pointer steps. Native pointer drags are supported;
browser-specific HTML5 drag-data behavior is not universally guaranteed.
`type` sends native key-down/character/key-up events for printable ASCII. Other
Unicode uses text insertion; this is not a real IME/composition guarantee.
`fill` retains fast text replacement. Selection sends synthetic `input` and
`change` events and verifies the final value; it does not claim trusted native selection.

Click/key/drag actions accept modifier names ALT/CONTROL/META/SHIFT. Ordinary
editing and Ctrl+A/Z/Y are automatic in balanced-v3. Modified activation and
unknown drag effects still require approval. Ctrl/Meta+C/V/X are blocked: they
would access a process/global clipboard. Use `browser_clipboard` instead.
Key and mouse releases run even on failures. A modal dialog that prevents CDP
release defers only that release until the dialog response; the action is never
re-dispatched. Closing the browser also discards its input state.

Balanced-v3 also permits non-form popup buttons tied to an existing menu/dialog/listbox
or native popover, and a narrow set of canvas-editor tool/help controls. Tool selection
needs an observed pressed/radio state, a toolbar/group or keyboard shortcut, a nearby
visible canvas with multiple tool controls, and a recognized tool name. Help needs
that editor context plus an F1/? shortcut. Names or `aria-pressed` alone are insufficient.
Account/privacy/permission context, submission, transfers and unknown effects still
require approval. Image/upload, eraser/delete and generic Enter submission are not
covered. This is a bounded UX heuristic, not proof that arbitrary JS cannot save or
transmit data. Strict remains operator-selectable; existing configuration names are unchanged.

## Dialogs and diagnostics

`browser_dialog(operation="get")` returns an opaque dialog_id and bounded
message. `accept`/`dismiss` require that ID and an actual private-console approval;
echoing a token is insufficient. Prompt text is bounded; sensitive prompts use
private authentication. No automatic blanket acceptance is performed.

`browser_logs` returns at most 64 event records with sequence cursors. It keeps
console levels and exception events/locations, **not arbitrary console arguments,
stack locals or exception object dumps**. They can contain otherwise undetectable
credentials. Logs are cleared and collection is disabled during authentication
or manual control. This is a bounded diagnostic journal, not a developer-console
replacement.

## Files and work-local clipboard

`browser_artifacts` supports list/get/delete/clear/export. Files have opaque IDs,
work-local directories, a 32-item limit, operator byte budgets and expiry.
Defaults: 16 MiB per file, 64 MiB total, 30-minute retention. Download progress
rejects known oversize totals and cancels on observed quota excess; network
progress notifications are not a filesystem hard quota. No path is accepted.
Closing a session removes its registered artifacts. Unclean worker termination
can leave quarantined files on disk. The next session's bounded expiry sweep
removes old generated artifact files only, skipping live work, symlinks and
unknown names. With no subsequent session, operator cleanup may still be needed;
it must not mistake artifact files for user profiles or authentication databases.

Exports are bounded rendered text, a new inert escaped-HTML document, or an image
from the existing privacy-checked capture pipeline. Source truncation is marked.
MCP get returns scrubbed UTF-8 text or exported image content. Downloaded binary
files and unchecked downloaded images are not exposed as AI-visible image bytes.
The private console offers authenticated, CSRF-protected attachment downloads
without executing or previewing the file. Artifacts are untrusted website data.

The file-chooser event exposes its exact input node ID, including an intercepted
hidden input. Prepare files in the private console, then use `upload_ids` with
the existing approved upload action. No local-PC files or arbitrary server paths
are automatically selected. Auth/manual control disables picker interception.

`browser_clipboard` provides read/write/clear and observed-node copy/paste. It is
an in-memory text buffer per work lease, never the user's PC or server clipboard.
Paste follows the same input policy as fill. Authentication, handoff and session
termination clear the buffer.
Entering and completing private control invalidates that work's outstanding
approvals, issued nodes, screenshots and cursors, even if the target looks unchanged.
Transition failure does not restore earlier consent. Execution deduplication
records remain intact; manual control does not authorize a dispatch retry.

## Operator-pinned WebMCP reads and URLs

Only operator configuration `CB_WEBMCP_READ_ALLOWLIST` can authorize automatic
page-tool reads. Its shape is `{ "https://example.com": {"search_tool": "64-char
schema SHA-256"} }`. `browser_list_page_tools` provides `schema_sha256` for operator
review. The digest uses sorted-key, ASCII-escaped compact JSON. Origin, tool name
and schema must all match; a page's own `readOnly` hint is not authority. Changed
registration/schema requires re-observation or confirmation. The operator must
independently determine that the tool is appropriate for preauthorization.
Page-provided functions are unavailable when protected regions or uninspectable
frames are present: unlike filtered DOM observation, arbitrary page callbacks
can collect hidden private values. Use the filtered observation interface instead.

URL output now preserves common public search/navigation parameters and document
anchors. Unknown query parameters, credentials, authentication fragments and
recognizable secrets remain masked. A sanitized URL is not guaranteed reusable as
the exact original navigation input. The server's `error.code` identifies the
cause; an MCP client's outer error text may be a wrapper around it.
