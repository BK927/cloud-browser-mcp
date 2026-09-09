import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .authentication import AuthRule


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
    oauth_client_id: str = "personal-cloud-browser"
    oauth_redirect_uris: list[str] = Field(default_factory=list)
    access_ttl: int = Field(900, ge=60, le=3600)
    refresh_ttl: int = Field(604800, ge=600, le=2592000)
    session_ttl: int = Field(3600, ge=60)
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
    navigation_timeout: float = Field(20, ge=1, le=30)
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
    headless: bool = False
    # Inspect ordinary frames; mask sensitive/uninspectable regions. The legacy
    # stronger block/all-mask operator choices remain available.
    iframe_screenshot_policy: Literal["inspect", "block", "mask"] = "inspect"
    novnc_dir: Path = Path("/usr/share/novnc")
    vnc_websocket: str = "ws://127.0.0.1:6080/websockify"
    # Off by default: deploying the MCP never implicitly enables remote desktop.
    manual_control_enabled: bool = False

    @model_validator(mode="after")
    def safe_deployment(self):
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
