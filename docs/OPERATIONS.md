# Expanded operations (contract 0.4 draft)

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
32MiB plus 16 bytes per pixel (full-page requests use the configured maximum
pixel count). These are admission estimates, not per-operation hard caps or
measured guarantees. `strict` retains the normal reserve for every admitted task.
PSI's worst host/cgroup avg10 is reported; full stalls >=10% or some stalls >=50%
deny new admitted work. PSI absence is reported as unknown, never zero.
Small fresh text observations and explicit cleanup remain available under
pressure. Auto observations fall back to bounded interactive text; a denied
visual request returns RESOURCE_PRESSURE rather than pretending to return an image.

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

`browser_observe.query` accepts `frame_id`, CSS `scope` and `selector`, `role`,
`name`, `label`, and `limit` (1–100). Scope/selector only locate observed DOM
objects; subsequent actions require the returned backend-bound node IDs.
Name/label/role filters are case-insensitive substring filters on observed
metadata. Only visible viewport nodes are actionable. Semantic CSS queries
read the first matching rendered subtree. Query limits and partial frames are
reported, not represented as a complete DOM dump. Omitting query on a new
observation resets its scope; a cursor retains its original snapshot.

`browser_wait` accepts a condition with type `url` (exact `value`), `element`
(`query`, state present/absent/visible/hidden/enabled), `dialog`, or `download`
(completed). Waits are bounded to 10 seconds; timeout is `no_change` with
`wait.timed_out=true`. A hidden/absent element condition means absent from the
visible, bounded query result, not proof that no matching DOM node exists.
Use narrow selectors; a partial observation is not a global absence proof.

`browser_act.completion` uses the same condition and optional
`completion_timeout_ms`. `follow_up=true` requests at most 2,000 characters of
interactive observation. A failed completion wait does not erase the fact that
the action was dispatched. Never repeat a submitted action because its wait
timed out. Poll `browser_status(operation_id=...)` after a lost HTTP response.

## Keyboard and mouse

Added action types: `type` (append sequential Unicode text), `right_click`,
`middle_click`, `drag` (source `node_id`, destination `target_node_id`), and
`select_multiple` (`values`). Typing's total artificial delay is at most 8 s;
drag uses 2–30 bounded pointer steps. Native pointer drags are supported;
browser-specific HTML5 drag-data behavior is not universally guaranteed.

Click/key/drag actions accept modifier names ALT/CONTROL/META/SHIFT. Ordinary
editing and Ctrl+A/Z/Y are automatic in balanced-v2. Modified activation and
unknown drag effects still require approval. Ctrl/Meta+C/V/X are blocked: they
would access a process/global clipboard. Use `browser_clipboard` instead.
Key and mouse releases run even on failures. A modal dialog that prevents CDP
release defers only that release until the dialog response; the action is never
re-dispatched. Closing the browser also discards its input state.

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

## Operator-pinned WebMCP reads and URLs

Only operator configuration `CB_WEBMCP_READ_ALLOWLIST` can authorize automatic
page-tool reads. Its shape is `{ "https://example.com": {"search_tool": "64-char
schema SHA-256"} }`. `browser_list_page_tools` provides `schema_sha256` for operator
review. The digest uses sorted-key, ASCII-escaped compact JSON. Origin, tool name
and schema must all match; a page's own `readOnly` hint is not authority. Changed
registration/schema requires re-observation or confirmation. The operator must
independently determine that the tool is appropriate for preauthorization.

URL output now preserves common public search/navigation parameters and document
anchors. Unknown query parameters, credentials, authentication fragments and
recognizable secrets remain masked. A sanitized URL is not guaranteed reusable as
the exact original navigation input. The server's `error.code` identifies the
cause; an MCP client's outer error text may be a wrapper around it.
