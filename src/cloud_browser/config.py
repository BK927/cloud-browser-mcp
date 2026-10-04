import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .authentication import AuthRule

LANGUAGE_TAG = re.compile(r"[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CB_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    data_dir: Path = Path("data")
    public_origin: str = "http://127.0.0.1:8000"
    public_path_prefix: str = Field("", max_length=128)
    control_origin: str = "http://127.0.0.1:8001"
    bind_host: str = "127.0.0.1"
    public_port: int = 8000
    control_port: int = 8001
    development: bool = False
    http_diagnostics: bool = False
    network_isolated: bool = False
    admin_password_hash: str = ""
    passkey_bridge_secret: str = Field("", exclude=True, repr=False, max_length=256)
    oauth_client_id: str = "personal-cloud-browser"
    oauth_redirect_uris: list[str] = Field(default_factory=list)
    access_ttl: int = Field(900, ge=60, le=3600)
    refresh_ttl: int = Field(2592000, ge=600, le=2592000)
    grant_max_ttl: int = Field(7776000, ge=600, le=15552000)
    session_ttl: int = Field(3600, ge=60)
    control_session_ttl: int = Field(28800, ge=900, le=86400)
    session_sweep_interval: float = Field(15, ge=1, le=300)
    command_queue_timeout: float = Field(46, ge=1, le=60)
    max_queued_per_work: int = Field(4, ge=1, le=16)
    approval_ttl: int = Field(120, ge=15, le=600)
    approval_policy: Literal["strict", "balanced"] = "strict"
    handoff_ttl: int = Field(600, ge=30, le=3600)
    memory_reserve_mb: int = Field(256, ge=32)
    memory_per_tab_mb: int = Field(96, ge=16)
    memory_policy: Literal["adaptive", "strict"] = "adaptive"
    memory_floor_mb: int = Field(96, ge=32)
    memory_per_session_mb: int = Field(192, ge=64)
    max_sessions: int = Field(2, ge=1, le=8)
    max_capture_pixels: int = Field(8_000_000, ge=786432)
    node_registry_bytes: int = Field(2 * 1024 * 1024, ge=64 * 1024, le=16 * 1024 * 1024)
    navigation_timeout: float = Field(60, ge=1, le=300)
    navigation_max_timeout: float = Field(300, ge=1, le=300)
    navigation_min_interval_ms: int = Field(1500, ge=0, le=60000)
    navigation_per_host_per_minute: int = Field(30, ge=1, le=600)
    reader_enabled: bool = True
    reader_idle_ttl: int = Field(300, ge=60, le=3600)
    reader_timeout: float = Field(25, ge=5, le=40)
    reader_max_text_chars: int = Field(60000, ge=4000, le=250000)
    auth_rules: dict[str, AuthRule] = Field(default_factory=dict)
    webmcp_enabled: bool = True
    webmcp_testing: bool = False
    webmcp_read_allowlist: dict[str, dict[str, str]] = Field(default_factory=dict)
    max_artifact_mb: int = Field(64, ge=4, le=512)
    max_artifact_file_mb: int = Field(16, ge=1, le=100)
    artifact_ttl: int = Field(1800, ge=60, le=86400)
    browser_group: str = "browser"
    managed_display: bool = False
    runtime_dir: Path = Path("/run/cloud-browser")
    display_number: int = Field(99, ge=10, le=999)
    display_width: int = Field(1920, ge=1024, le=3840)
    display_height: int = Field(1440, ge=768, le=2160)
    vnc_port: int = Field(5900, ge=1024, le=65535)
    vnc_bridge_port: int = Field(6080, ge=1024, le=65535)
    browser_cleanup_command: str = ""
    native_config: Path | None = None
    max_upload_mb: int = Field(16, ge=1, le=100)
    max_staged_uploads: int = Field(8, ge=1, le=32)
    upload_ttl: int = Field(600, ge=60, le=3600)
    chromium_path: str = "/usr/bin/chromium"
    browser_proxy: str = "http://egress:3128"
    browser_language: str | None = None
    browser_accept_language: str | None = Field(None, max_length=256)
    browser_timezone: str | None = Field(None, max_length=128)
    headless: bool = False
    # Inspect ordinary frames; mask sensitive/uninspectable regions. The legacy
    # stronger block/all-mask operator choices remain available.
    iframe_screenshot_policy: Literal["inspect", "block", "mask"] = "inspect"
    novnc_dir: Path = Path("/usr/share/novnc")
    vnc_websocket: str = "ws://127.0.0.1:6080/websockify"
    # Off by default: deploying the MCP never implicitly enables remote desktop.
    manual_control_enabled: bool = False

    @field_validator("browser_language", "browser_accept_language")
    @classmethod
    def valid_browser_languages(cls, value, info):
        if value is None:
            return value
        tags = value.split(",") if info.field_name == "browser_accept_language" else [value]
        if len(tags) > 10 or any(not LANGUAGE_TAG.fullmatch(tag) for tag in tags):
            raise ValueError("Browser languages must be BCP-47 tags (at most 10 in the list)")
        return value

    @field_validator("browser_timezone")
    @classmethod
    def valid_browser_timezone(cls, value):
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            try:
                ZoneInfo("UTC")
            except ZoneInfoNotFoundError:
                # Windows development may have neither system tzdata nor the
                # Python tzdata package. Chromium still has its own zone database.
                if re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z][A-Za-z0-9_+-]*){1,2}", value):
                    return value
                raise ValueError("Browser timezone must be an Area/Location name") from None
            raise ValueError("Unknown browser timezone") from None
        return value

    @model_validator(mode="after")
    def safe_deployment(self):
        if self.browser_accept_language is None and self.browser_language is not None:
            self.browser_accept_language = (
                self.browser_language + "," + self.browser_language.split("-")[0]
            )
        if self.navigation_timeout > self.navigation_max_timeout:
            raise ValueError("Default navigation timeout exceeds the operator ceiling")
        if self.reader_timeout > self.navigation_max_timeout:
            raise ValueError("Reader timeout exceeds the operator navigation ceiling")
        if self.grant_max_ttl < self.refresh_ttl:
            raise ValueError("OAuth grant lifetime must cover the refresh token lifetime")
        if self.display_number + self.max_sessions > 1000:
            raise ValueError("Display range must fit all configured work slots")
        for site, tools in self.webmcp_read_allowlist.items():
            parsed_site = urlsplit(site)
            if (
                parsed_site.scheme not in ("http", "https")
                or not parsed_site.hostname
                or parsed_site.path
                or parsed_site.query
                or parsed_site.fragment
                or parsed_site.username
            ):
                raise ValueError("WebMCP allowlist keys must be exact origins")
            if not self.development and parsed_site.scheme != "https":
                raise ValueError("Production WebMCP allowlist requires HTTPS")
            if len(tools) > 64 or any(
                not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in tools.values()
            ):
                raise ValueError("WebMCP tool allowlist needs exact SHA-256 schema fingerprints")
        prefix = self.public_path_prefix
        if prefix and (
            not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)+", prefix)
            or any(part in (".", "..") for part in prefix.split("/"))
        ):
            raise ValueError(
                "Public prefix must be empty or an unescaped absolute path without trailing slash or dot segments"
            )
        for site in self.auth_rules:
            parsed = urlsplit(site)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
                or (not self.development and parsed.scheme != "https")
            ):
                raise ValueError("Authentication rule keys must be exact site origins")
        if self.public_port == self.control_port:
            raise ValueError("Public MCP and private control need separate listeners")
        for origin in (self.public_origin, self.control_origin):
            parsed = urlsplit(origin)
            if (
                not parsed.hostname
                or parsed.scheme not in ("http", "https")
                or parsed.query
                or parsed.fragment
                or parsed.path not in ("", "/")
                or parsed.username
            ):
                raise ValueError("Origins must contain scheme, host and optional port only")
            if not self.development and parsed.scheme != "https":
                raise ValueError("Production origins must use HTTPS")
        if not self.development:
            if not self.admin_password_hash or not self.oauth_redirect_uris:
                raise ValueError("Configure administrator hash and exact OAuth callback URLs")
            if not self.network_isolated or not self.browser_proxy:
                raise ValueError("Production requires isolated browser networking and egress proxy")
            if not self.admin_password_hash.startswith("$argon2id$"):
                raise ValueError("Administrator password must be an Argon2id hash")
        if self.public_origin.rstrip("/") == self.control_origin.rstrip("/"):
            raise ValueError("Public and private origins must differ")
        for uri in self.oauth_redirect_uris:
            parsed = urlsplit(uri)
            if (
                "*" in uri
                or "#" in uri
                or "\\" in uri
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in uri)
                or not parsed.hostname
                or parsed.port == 0
                or parsed.username is not None
                or parsed.password is not None
                or parsed.scheme != "https"
            ):
                raise ValueError("OAuth callbacks must be exact HTTPS URLs without fragments")
        self.public_origin = self.public_origin.rstrip("/")
        self.control_origin = self.control_origin.rstrip("/")
        return self

    @property
    def public_base(self) -> str:
        return self.public_origin + self.public_path_prefix

    @property
    def issuer(self) -> str:
        return self.public_base

    @property
    def resource(self) -> str:
        return self.public_base + "/mcp"

    @property
    def authorization_path(self) -> str:
        return self.public_path_prefix + "/authorize"

    @property
    def resource_metadata_url(self) -> str:
        return (
            self.public_origin
            + "/.well-known/oauth-protected-resource"
            + self.public_path_prefix
            + "/mcp"
        )

    @property
    def issuer_metadata_url(self) -> str:
        return (
            self.public_origin + "/.well-known/oauth-authorization-server" + self.public_path_prefix
        )
