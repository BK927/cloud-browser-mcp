from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeAction(StrictModel):
    type: Literal["click", "double_click"]
    node_id: str


class FillAction(StrictModel):
    type: Literal["fill"]
    node_id: str
    text: str = Field(max_length=20000)


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
        ]
    ] = Field(min_length=1, max_length=1)


class SelectAction(StrictModel):
    type: Literal["select"]
    node_id: str
    value: str = Field(max_length=2000)


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
    | UploadAction,
    Field(discriminator="type"),
]


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
