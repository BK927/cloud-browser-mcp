"""Static, payload-free error descriptions and recovery hints."""

ERROR_CATEGORIES = {
    "stale_state": "STALE_NODE STALE_REVISION STALE_SCREENSHOT CURSOR_STALE FRAME_STALE SCREEN_CHANGED DOM_TARGET_AVAILABLE",
    "capacity": "BROWSER_BUSY RESOURCE_PRESSURE CAPTURE_TIMEOUT WORKER_TIMEOUT CLEANUP_REQUIRED",
    "approval": "CONFIRMATION_REQUIRED CONFIRMATION_STALE CONFIRMATION_USED CONFIRMATION_DENIED ACTION_ALREADY_DISPATCHED",
    "navigation": "NAVIGATION_TIMEOUT NAVIGATION_FAILED NAVIGATION_CANCELLED NAVIGATION_IN_PROGRESS",
    "target": "NODE_NOT_ACTIONABLE NODE_AMBIGUOUS NODE_NOT_FOUND TAB_NOT_FOUND FRAME_UNAVAILABLE ACTION_GOAL_NOT_MET",
    "input": "INVALID_INPUT INVALID_URL INVALID_SELECTOR INVALID_COORDINATES READ_NOT_FOUND LEASE_REQUIRED LEASE_INVALID OPERATION_CONFLICT UNSUPPORTED_OPERATION",
    "page_limit": "PRIVACY_INSPECTION_INCOMPLETE",
    "privacy_guard": "SENSITIVE_SCREEN SENSITIVE_INPUT SENSITIVE_TARGET SENSITIVE_CONTENT POLICY_BLOCKED",
    "site_challenge": "CAPTCHA_REQUIRED BOT_BLOCKED AUTH_REQUIRED",
    "human_control": "USER_CONTROL_ACTIVE AUTH_IN_PROGRESS HANDOFF_UNAVAILABLE",
    "session": "SESSION_EXPIRED SESSION_NOT_FOUND SESSION_CLOSED",
    "uncertain": "RESULT_UNCERTAIN",
}
CODE_CATEGORY = {
    code: category for category, codes in ERROR_CATEGORIES.items() for code in codes.split()
}
NEXT_STEPS = {
    "stale_state": "Observe again and retry with the new revision and node IDs.",
    "capacity": "Check browser status and wait for capacity or finish existing work before retrying.",
    "approval": "Ask the user to decide in the private console; resend the identical action with the confirmation token only after approval.",
    "navigation": "Observe the current page and check navigation progress before starting another navigation.",
    "target": "Observe again and select an available, actionable target matching the intended action.",
    "input": "Correct the request arguments and use the current server-issued identifiers.",
    "page_limit": "The page exceeds the bounded privacy scan; this is a size limit, not a detected secret.",
    "privacy_guard": "A privacy guard withheld this content; it is not a site block. Use text observation or a narrower selector.",
    "site_challenge": "The site requires human verification; ask the user.",
    "human_control": "Ask the user to finish or return control in the private console, then check browser status.",
    "session": "Open a new browser session and retain its lease before continuing.",
    "uncertain": "Do not repeat the action; ask the user to confirm the outcome.",
    "browser": "Check browser status and inspect the current state before choosing another action.",
}
PAGE_LIMIT_MESSAGE = "Page exceeds the bounded privacy scan (element/input budget); size limit, not a detected secret"


def error_metadata(code, *, handoff_available=False):
    category = CODE_CATEGORY.get(code, "browser")
    suggested_tool = {
        "AUTH_REQUIRED": "browser_auth_request",
        "SENSITIVE_SCREEN": "browser_observe",
        "READ_NOT_FOUND": "browser_read",
        "LEASE_REQUIRED": "browser_open",
    }.get(code, "browser_status")
    if category in ("stale_state", "target", "navigation") or code in (
        "CONFIRMATION_STALE",
        "ACTION_ALREADY_DISPATCHED",
        "CONFIRMATION_USED",
    ):
        suggested_tool = "browser_observe"
    elif category == "session":
        suggested_tool = "browser_open"
    elif code in (
        "RESULT_UNCERTAIN",
        "CAPTCHA_REQUIRED",
        "BOT_BLOCKED",
        "PRIVACY_INSPECTION_INCOMPLETE",
    ):
        suggested_tool = (
            "browser_handoff"
            if handoff_available
            else "browser_close"
            if code == "RESULT_UNCERTAIN"
            else None
        )
    return {
        "category": category,
        "retryable": category == "stale_state"
        or code in ("BROWSER_BUSY", "RESOURCE_PRESSURE", "CAPTURE_TIMEOUT", "CONFIRMATION_STALE"),
        "suggested_tool": suggested_tool,
        "next_step": NEXT_STEPS[category],
    }
