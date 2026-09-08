# Expanded operations (contract 0.4 draft)

All session calls require the authenticated work's `lease_id`. The MCP tool
schema is authoritative for input bounds. These additions use the existing
worker, node validation, human-control lock and approval/execution journal.
There is no arbitrary JavaScript evaluation tool.

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
