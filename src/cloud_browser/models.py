from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeAction(StrictModel):
    type: Literal["click", "double_click", "right_click", "middle_click"]
    node_id: str
    modifiers: list[Literal["ALT", "CONTROL", "META", "SHIFT"]] = Field(
        default_factory=list, max_length=4
    )


class FillAction(StrictModel):
    type: Literal["fill"]
    node_id: str
    text: str = Field(max_length=20000)


class TypeAction(StrictModel):
    type: Literal["type"]
    node_id: str
    text: str = Field(max_length=2000)
    interval_ms: int = Field(0, ge=0, le=20)

    @model_validator(mode="after")
    def bounded_delay(self):
        if len(self.text) * self.interval_ms > 8000:
            raise ValueError("Sequential typing delay must total at most 8 seconds")
        return self


class KeyAction(StrictModel):
    type: Literal["keypress"]
    node_id: str
    keys: list[
        Literal[
            "ENTER",
            "TAB",
            "ESCAPE",
            "SPACE",
            "ARROWUP",
            "ARROWDOWN",
            "ARROWLEFT",
            "ARROWRIGHT",
            "BACKSPACE",
            "DELETE",
            "HOME",
            "END",
            "PAGEUP",
            "PAGEDOWN",
            "A",
            "B",
            "C",
            "D",
            "E",
            "F",
            "G",
            "H",
            "I",
            "J",
            "K",
            "L",
            "M",
            "N",
            "O",
            "P",
            "Q",
            "R",
            "S",
            "T",
            "U",
            "V",
            "W",
            "X",
            "Y",
            "Z",
        ]
    ] = Field(min_length=1, max_length=1)
    modifiers: list[Literal["ALT", "CONTROL", "META", "SHIFT"]] = Field(
        default_factory=list, max_length=4
    )


class SelectAction(StrictModel):
    type: Literal["select"]
    node_id: str
    value: str = Field(max_length=2000)


class MultiSelectAction(StrictModel):
    type: Literal["select_multiple"]
    node_id: str
    values: list[str] = Field(min_length=0, max_length=50)


class DragAction(StrictModel):
    type: Literal["drag"]
    node_id: str
    target_node_id: str
    steps: int = Field(12, ge=2, le=30)
    modifiers: list[Literal["ALT", "CONTROL", "META", "SHIFT"]] = Field(
        default_factory=list, max_length=4
    )


class UploadAction(StrictModel):
    type: Literal["upload"]
    node_id: str
    upload_ids: list[str] = Field(min_length=1, max_length=8)


class CheckAction(StrictModel):
    type: Literal["check"]
    node_id: str
    checked: bool


class ScrollAction(StrictModel):
    type: Literal["scroll"]
    node_id: str | None = None
    delta_x: int = Field(0, ge=-5000, le=5000)
    delta_y: int = Field(0, ge=-5000, le=5000)


class CoordinateAction(StrictModel):
    type: Literal["click_at", "double_click_at", "move_to", "scroll_at"]
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    screenshot_id: str
    delta_x: int = Field(0, ge=-5000, le=5000)
    delta_y: int = Field(0, ge=-5000, le=5000)


Action = Annotated[
    NodeAction
    | FillAction
    | KeyAction
    | SelectAction
    | CheckAction
    | ScrollAction
    | CoordinateAction
    | UploadAction
    | TypeAction
    | MultiSelectAction
    | DragAction,
    # New variants retain the same strict discriminated contract.
    Field(discriminator="type"),
]


class ObservationQuery(StrictModel):
    frame_id: str | None = None
    scope: str | None = Field(None, max_length=1000)
    selector: str | None = Field(None, max_length=1000)
    role: str | None = Field(None, max_length=100)
    name: str | None = Field(None, max_length=500)
    label: str | None = Field(None, max_length=500)
    limit: int = Field(60, ge=1, le=100)


class WaitCondition(StrictModel):
    type: Literal["url", "element", "dialog", "download"]
    value: str | None = Field(None, max_length=2000)
    query: ObservationQuery | None = None
    state: Literal["present", "absent", "visible", "hidden", "enabled", "completed"] = "present"

    @model_validator(mode="after")
    def required_condition(self):
        if self.type == "url" and not self.value:
            raise ValueError("URL condition requires an exact URL")
        if self.type == "element" and not self.query:
            raise ValueError("Element condition requires a query")
        return self


class Configuration(StrictModel):
    viewport_width: int | None = Field(None, ge=320, le=1920)
    viewport_height: int | None = Field(None, ge=240, le=1440)
    screenshot_quality: int | None = Field(None, ge=25, le=95)
    max_chars: int | None = Field(None, ge=256, le=100000)
    wait_ms: int | None = Field(None, ge=0, le=10000)

    @model_validator(mode="after")
    def paired_viewport(self):
        if (self.viewport_width is None) != (self.viewport_height is None):
            raise ValueError("Both viewport dimensions are required")
        return self


class BrowserError(Exception):
    def __init__(self, code: str, message: str, status: str = "error", **details):
        self.code, self.message, self.status, self.details = code, message, status, details
        super().__init__(message)


def response(
    status="ok",
    *,
    request_id=None,
    session_id=None,
    tab_id=None,
    revision=None,
    page=None,
    notices=None,
    error=None,
    **extra,
):
    import secrets

    return dict(
        status=status,
        request_id=request_id or "req_" + secrets.token_urlsafe(12),
        session_id=session_id,
        tab_id=tab_id,
        revision=revision,
        page=page,
        notices=notices or [],
        error=error,
        **extra,
    )
