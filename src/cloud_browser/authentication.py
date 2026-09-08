"""Operator-declared authentication evidence, never credentials or AI-supplied rules."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AuthMethod = Literal["password", "email_code", "sso", "passkey", "security_key"]
MANUAL_METHODS = {"password", "email_code", "sso"}


class AuthRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supported_methods: list[AuthMethod] = Field(
        default_factory=lambda: ["password", "email_code", "sso"], min_length=1, max_length=5
    )
    success_selector: str | None = Field(None, min_length=1, max_length=500)
    failure_selector: str | None = Field(None, min_length=1, max_length=500)
