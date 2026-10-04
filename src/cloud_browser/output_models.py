"""Public structured-result contracts; validation never rewrites tool results.

Method-specific fields are optional because errors, human-control requests and
operation replays share the same envelope. Nullable fields retain the existing
wire representation. Extra provider/error metadata remains forward compatible.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _compact_schema(schema: dict[str, Any]) -> None:
    """Remove generated display noise, visiting schemas rather than property names."""
    schema.pop("title", None)
    if schema.get("additionalProperties") is True:
        schema.pop("additionalProperties")  # JSON Schema's default; retain false/schema values.
    if schema.get("default", ...) is None:
        schema.pop("default")
    choices = schema.get("anyOf")
    if (
        isinstance(choices, list)
        and choices
        and all(
            isinstance(child, dict) and set(child) == {"type"} and isinstance(child["type"], str)
            for child in choices
        )
    ):
        # Equivalent JSON Schema type union; keep constraints, refs and enums intact.
        schema["type"] = list(dict.fromkeys(child["type"] for child in choices))
        schema.pop("anyOf")
    for keyword in ("properties", "$defs", "patternProperties", "dependentSchemas"):
        for child in schema.get(keyword, {}).values():
            if isinstance(child, dict):
                _compact_schema(child)
    for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
        for child in schema.get(keyword, []):
            if isinstance(child, dict):
                _compact_schema(child)
    for keyword in (
        "items",
        "contains",
        "additionalProperties",
        "unevaluatedProperties",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
    ):
        child = schema.get(keyword)
        if isinstance(child, dict):
            _compact_schema(child)


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, json_schema_extra=_compact_schema)


class Page(OutputModel):
    url: str
    title: str | None


class Error(OutputModel):
    code: str
    message: str
    retryable: bool | None = None
    suggested_tool: str | None = None
    category: str | None = None
    next_step: str | None = None


class CaptureOmission(Error):
    reason: str | None = None
    capture_reasons: list[str] | None = None
    capture_attempts: int | None = None


class BrowserOutput(OutputModel):
    status: Literal[
        "ok", "no_change", "error", "blocked", "confirmation_required", "user_action_required"
    ]
    request_id: str
    session_id: str | None
    tab_id: str | None
    selected_tab_id: str | None = None
    revision: int | None
    page: Page | None
    notices: list[str]
    error: Error | None
    page_cached: bool | None = None
    retry_after_seconds: int | None = None
    termination_reason: str | None = None
    busy_reason: str | None = None


class NavigationDetails(OutputModel):
    url_changed: bool | None = None
    document_changed: bool | None = None
    navigation_kind: Literal["none", "full_document", "same_document", "reload"] | None = None
    same_document_kind: Literal["hash", "history_api", "other"] | None = None


class Navigation(NavigationDetails):
    operation: Literal["goto", "back", "forward", "reload"] | None = None
    redirected: bool | None = None
    navigation_occurred: bool | None = None
    pending: bool | None = None
    operation_id: str | None = None
    phase: (
        Literal[
            "command_response",
            "document_transition",
            "loading",
            "final_verification",
            "completed",
            "cancelled",
        ]
        | None
    ) = None
    elapsed_ms: int | None = None
    timeout_ms: int | None = None


class OpenOutput(BrowserOutput):
    operation_id: str | None = None
    lease_id: str | None = Field(None, description="Keep work lease for calls.")
    expires_at: str | None = None
    navigation: Navigation | None = None
    current_page: Page | None = None


class Tab(Page):
    tab_id: str
    selected: bool | None = None


class TabsOutput(BrowserOutput):
    selected_tab_id: str | None = None
    tabs: list[Tab] | None = None


class NavigateOutput(BrowserOutput):
    operation_id: str | None = None
    navigation: Navigation | None = None
    current_page: Page | None = None


class Viewport(OutputModel):
    width: float
    height: float


class Region(Viewport):
    x: float
    y: float


class Screenshot(Viewport):
    screenshot_id: str = Field(description="Coordinate action ID; image in content.")
    coordinate_units: str
    full_page: bool
    masked_regions: list[Region]
    captured_at: float
    capture_attempts: int | None = None


class Frame(OutputModel):
    frame_id: str | None
    parent_frame_id: str | None
    origin: str | None = None
    readable: bool
    actionable: bool
    reason: str | None


class FileChooser(OutputModel):
    node_id: str | None = None
    frame_id: str | None = None
    multiple: bool | None = None
    error: str | None = None


class Observation(OutputModel):
    semantic_snapshot: str | None = None
    interactive_snapshot: str | None = Field(
        None, description="Node JSON lines with action node_id."
    )
    truncated: bool | None = None
    next_cursor: str | None = Field(None, description="Revision-bound cursor.")
    semantic_truncated: bool | None = None
    interactive_page_truncated: bool | None = None
    interactive_truncated: bool | None = None
    query_scan_truncated: bool | None = None
    query_match_count: int | None = None
    query_empty_reason: str | None = None
    protected_regions_omitted: bool | None = None
    observation_revision: int | None = None
    pagination_stale: bool | None = None
    screenshot_omitted: CaptureOmission | None = None
    semantic_source: str | None = None
    semantic_source_truncated: bool | None = None
    accessibility_source: str | None = None
    scroll_scan_truncated: bool | None = None
    readable_frames: int | None = None
    frame_reading_truncated: bool | None = None
    frames: list[Frame] | None = None
    file_chooser: FileChooser | None = None
    viewport: Viewport | None = None
    screenshot: Screenshot | None = None
    resource_limited: bool | None = None
    error: Error | None = None


class ObserveOutput(BrowserOutput):
    observation: Observation | None = None


class ReadLink(OutputModel):
    text: str
    url: str


class ReadDetails(OutputModel):
    read_id: str
    complete: bool
    text: str
    offset: int
    next_offset: int | None
    total_chars: int
    text_capped: bool
    links: list[ReadLink]
    links_truncated: bool
    resource_limited: bool
    protected_regions_omitted: bool
    frame_reading_truncated: bool | None = None
    loaded: list[Literal["images", "video", "fonts"]]
    blocked_requests: int
    screenshot_omitted: dict[str, str] | None = None


class ReadOutput(BrowserOutput):
    read: ReadDetails | None = None


class ActionPolicy(OutputModel):
    mode: str
    approval_required: bool
    reason: str


class Upload(OutputModel):
    upload_id: str
    filename: str
    display_name: str | None = None
    size: int
    sha256: str
    expires_at: str | None = None


class SentField(OutputModel):
    name: str | None = None
    type: str | None = None
    value: Any = None  # Page form metadata may use scalar or array values.


class Confirmation(OutputModel):
    confirmation_token: str = Field(description="Single-use console-approved token.")
    approval_state: Literal["pending", "approved"]
    summary: str
    current_page: str
    destination: str | None
    destination_kind: str
    destination_verified: bool
    data_sent: list[str | SentField]
    data_sent_truncated: bool
    data_sent_verified: bool
    files: list[Upload]
    expires_at: str
    control_url: str
    action_policy: ActionPolicy | None = None


class ActionResult(NavigationDetails):
    performed: bool
    target_state_verified: bool | None = None
    typing_semantics: str | None = None
    selection_events: str | None = None
    page_changed: bool | None = None
    navigation_occurred: bool | None = None
    new_tab_ids: list[str] | None = None


class WaitResult(OutputModel):
    matched: bool
    timed_out: bool | None = None
    condition: Literal["url", "element", "dialog", "download"] | None = None
    partial: bool | None = None
    error: Error | None = None


class Dialog(OutputModel):
    dialog_id: str
    type: str
    message: str
    sensitive: bool
    url: str


class PageToolResult(OutputModel):
    tool_name: str
    output: Any = Field(description="Untrusted page-defined JSON.")
    untrusted: bool


class Operation(OutputModel):
    state: Literal["not_found", "running", "completed"]
    result: BrowserOutput | None = Field(None, description="Original completed response.")
    navigation: Navigation | None = None


class ActOutput(BrowserOutput):
    action_result: ActionResult | None = None
    confirmation: Confirmation | None = None
    action_policy: ActionPolicy | None = None
    completion: WaitResult | None = None
    follow_up: Observation | None = None
    dialog: Dialog | None = None
    page_tool_result: PageToolResult | None = None
    operation: Operation | None = None
    replayed: bool | None = None


class HumanControl(OutputModel):
    handoff_id: str
    session_id: str
    tab_id: str
    kind: Literal["auth", "manual"]
    reason: str
    state: str
    authenticated: bool | None
    verification: str
    site_origin: str | None
    expires_at: str
    control_url: str
    automation_paused: bool
    control_access_expired: bool
    supported_methods: list[str] | None = None
    unsupported_methods: list[str] | None = None
    methods_scope: str | None = None
    site_methods_verified: bool | None = None
    site_methods_configured: bool | None = None
    start_error: str | None = None


class AuthOutput(BrowserOutput):
    auth: HumanControl | None = None


class HandoffOutput(BrowserOutput):
    handoff: HumanControl | None = None


class CloseOutput(BrowserOutput):
    selected_tab_id: str | None = None
    session_closed: bool | None = None


class MemoryConstraint(OutputModel):
    cgroup_path: str
    cgroup_limit_mb: int | None
    cgroup_used_mb: int | None
    available_mb: int
    accounting: str


class Resources(OutputModel):
    host_available_mb: int
    available_mb: int
    cgroup_limit_mb: int | None
    cgroup_used_mb: int | None
    cgroup_raw_headroom_mb: int | None
    cgroup_inactive_file_mb: int
    cgroup_reclaimable_estimate_mb: int
    cgroup_estimated_headroom_mb: int | None
    accounting: str
    cgroup_stat_status: str
    reserve_mb: int
    admission_mb: int
    can_admit: bool
    cgroup_path: str
    cgroup_constraints: list[MemoryConstraint]
    memory_pressure: dict[str, float] | None = None
    policy: str | None = None
    operation: str | None = None
    required_headroom_mb: int | None = None
    pressure_level: str | None = None
    soft_reserve_borrowed: bool | None = None


class Session(OutputModel):
    session_id: str
    expires_at: str
    work_lease_expired: bool
    result_uncertain: bool
    control: HumanControl | None
    tabs: list[Tab] | None
    selected_tab_id: str | None = None
    tabs_cached: bool | None = None
    work_state: str | None = None
    last_activity_at: str | None = None


class Approval(OutputModel):
    session_id: str
    tab_id: str
    state: str
    summary: str
    expires_at: str
    approval_state: str | None = None


class RecentError(OutputModel):
    tool: str
    code: str
    category: str
    request_id: str
    at: str


class Capabilities(OutputModel):
    approval_policy: str
    manual_control: bool
    webmcp: bool
    page_tools: str
    webmcp_runtime_check: str
    extended_input: list[str]
    clipboard: str
    artifacts: str
    installation: str
    browser_language: str | None = None
    browser_timezone: str | None = None
    navigation_default_timeout_ms: int | None = None
    navigation_max_timeout_ms: int | None = None
    navigation_min_interval_ms: int | None = None
    navigation_per_host_per_minute: int | None = None
    navigation_progress: str | None = None
    navigation_poll_max_hz: int | None = None


class Scheduler(OutputModel):
    state: str
    active_sessions: int
    max_sessions: int
    expired_sessions: int
    queued_commands: int
    running_commands: int
    automation_paused: bool
    retry_after_seconds: int
    can_open_session: bool | None = None
    owned_commands_can_queue: bool | None = None


class ReaderStatus(OutputModel):
    active: bool
    idle_expires_at: str | None


class StatusOutput(BrowserOutput):
    resources: Resources | None = None
    busy: bool | None = None
    sessions: list[Session] | None = None
    approvals: list[Approval] | None = None
    recent_errors: list[RecentError] | None = None
    staged_uploads: list[Upload] | None = None
    control_url: str | None = None
    capabilities: Capabilities | None = None
    operation: Operation | None = None
    scheduler: Scheduler | None = None
    navigations: list[Navigation] | None = None
    reader: ReaderStatus | None = None


class OutputConfiguration(OutputModel):
    viewport_width: int
    viewport_height: int
    screenshot_quality: int
    max_chars: int
    wait_ms: int
    navigation_timeout_ms: int | None = None


class ConfigureOutput(BrowserOutput):
    configuration: OutputConfiguration | None = None


class PageTool(OutputModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(description="Untrusted page-provided JSON Schema.")
    schema_sha256: str
    operator_read_approved: bool
    untrusted: bool


class PageToolsOutput(BrowserOutput):
    page_tools: list[PageTool] | None = None


class WaitOutput(BrowserOutput):
    wait: WaitResult | None = None


class LogRecord(OutputModel):
    sequence: int
    timestamp: float
    kind: str
    level: str
    url: str | None


class Logs(OutputModel):
    records: list[LogRecord]
    next_sequence: int
    truncated: bool
    lost_before: int | None
    payload_policy: str


class LogsOutput(BrowserOutput):
    logs: Logs | None = None


class Clipboard(OutputModel):
    text: str | None
    length: int
    scope: str


class ClipboardOutput(ActOutput):
    clipboard: Clipboard | None = None


class Artifact(OutputModel):
    artifact_id: str
    name: str
    mime_type: str
    kind: str
    state: str
    size: int
    created_at: float
    expires_at: float
    sha256: str | None = None
    source_truncated: bool | None = None


class ArtifactsOutput(BrowserOutput):
    artifacts: list[Artifact] | None = None
    artifact: Artifact | None = None
    removed_artifact_ids: list[str] | None = None
    text: str | None = None
    truncated: bool | None = None
    untrusted: bool | None = None
