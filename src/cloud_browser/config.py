from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CB_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    public_origin: str = "http://127.0.0.1:8000"
    control_origin: str = "http://127.0.0.1:8001"
    bind_host: str = "127.0.0.1"
    public_port: int = 8000
    control_port: int = 8001
    development: bool = False
    network_isolated: bool = False
    admin_password_hash: str = ""
    oauth_client_id: str = "personal-cloud-browser"
    oauth_redirect_uris: list[str] = Field(default_factory=list)
    access_ttl: int = Field(900, ge=60, le=3600)
    refresh_ttl: int = Field(604800, ge=600, le=2592000)
    session_ttl: int = Field(3600, ge=60)
    approval_ttl: int = Field(120, ge=15, le=600)
    handoff_ttl: int = Field(600, ge=30, le=3600)
    memory_reserve_mb: int = Field(256, ge=32)
    memory_per_tab_mb: int = Field(96, ge=16)
    max_sessions: int = Field(1, ge=1, le=8)
    max_capture_pixels: int = Field(8_000_000, ge=786432)
    chromium_path: str = "/usr/bin/chromium"
    browser_proxy: str = "http://egress:3128"
    headless: bool = False
    novnc_dir: Path = Path("/usr/share/novnc")
    vnc_websocket: str = "ws://127.0.0.1:6080/websockify"
    # Off by default: deploying the MCP never implicitly enables remote desktop.
    manual_control_enabled: bool = False

    @model_validator(mode="after")
    def safe_deployment(self):
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
            if "*" in uri or parsed.fragment or parsed.username or parsed.scheme != "https":
                raise ValueError("OAuth callbacks must be exact HTTPS URLs without fragments")
        self.public_origin = self.public_origin.rstrip("/")
        self.control_origin = self.control_origin.rstrip("/")
        return self

    @property
    def resource(self) -> str:
        return self.public_origin + "/mcp"
