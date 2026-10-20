import json
import tempfile
from functools import lru_cache

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings


class SeedServer(BaseModel):
    """An upstream registered at startup from trusted Helm config (never overwrites admin edits)."""

    slug: str
    name: str
    url: str
    auth_type: str = "none"
    auth_header_name: str | None = None
    credential_env: str | None = None
    timeout_seconds: int = 20
    tool_policies: dict[str, str] = {}


DEV_SESSION_SECRET = "dev-only-session-secret-change-me-0000"  # noqa: S105 - local-dev placeholder; startup warns when used
DEV_CHAT_SECRET = "dev-only-chat-token-secret-change-me-00"  # noqa: S105 - local-dev placeholder; startup warns when used
MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """Runtime configuration from environment variables; secrets come from K8s Secrets."""

    database_url: str
    credential_encryption_key: str = ""
    credential_encryption_keys: str = ""
    app_version: str = "dev"
    session_secret: str = DEV_SESSION_SECRET
    chat_token_secret: str = DEV_CHAT_SECRET
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    public_app_url: str = "http://app.127-0-0-1.nip.io"
    public_mcp_url: str = "http://mcp.127-0-0-1.nip.io/mcp"
    cookie_secure: bool = False
    session_ttl_seconds: int = 12 * 3600
    invite_ttl_hours: int = 72

    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"
    llm_api_key: str = ""
    llm_timeout_seconds: float = 45.0
    chat_token_ttl_seconds: int = 300
    chat_max_iterations: int = 6
    chat_max_tool_calls: int = 10
    chat_max_direct_tools: int = Field(default=20, ge=1)
    chat_turn_timeout_seconds: float = 90.0
    chat_tool_result_chars: int = 4000

    seed_servers: str = "[]"
    allowed_internal_hosts: str = ""
    allowed_mcp_hosts: str = "localhost:*,127.0.0.1:*"

    health_interval_seconds: float = 15.0
    health_timeout_seconds: float = 5.0
    unreachable_after_failures: int = 2
    max_concurrent_calls_per_upstream: int = 10
    default_call_timeout_seconds: int = 20
    max_result_bytes: int = Field(default=256 * 1024, ge=1024)
    rate_limit_calls_per_minute: int = Field(default=60, ge=1)
    rate_limit_burst: int = Field(default=20, ge=1)
    rate_limit_http_requests_per_minute: int = Field(default=600, ge=1)

    public_domain: str = "127-0-0-1.nip.io"
    public_scheme: str = "http"
    stores_max_per_user: int = Field(default=2, ge=1)
    stores_max_total: int = Field(default=3, ge=1)
    provisioner_timeout_minutes: int = Field(default=15, ge=1)
    provisioner_teardown_timeout_minutes: int = Field(default=5, ge=1)
    provisioner_poll_seconds: float = Field(default=3.0, ge=0.1)
    provisioner_concurrency: int = Field(default=2, ge=1, le=10)
    provisioner_lease_seconds: int = Field(default=60, ge=10)
    provisioner_lease_renew_seconds: int = Field(default=20, ge=1)
    provisioner_max_attempts: int = Field(default=3, ge=1)
    provisioner_chart_root: str = "/charts"
    provisioner_tenant_values: str = "/charts/woo-store/values-tenant.yaml"
    provisioner_temp_dir: str = Field(default_factory=tempfile.gettempdir)
    provisioner_tls_enabled: bool = False
    provisioner_tls_issuer: str = ""
    provisioner_woo_mcp_image: str = "urumi/woo-mcp:dev"
    provisioner_metrics_port: int = Field(default=9100, ge=1024, le=65535)
    hosted_mcp_allowed_registries: str = "ghcr.io/sajjad01-chaus,docker.io/mcp"

    @field_validator("session_secret", "chat_token_secret")
    @classmethod
    def strong_secret(cls, value: str) -> str:
        # HS256 needs at least a 256-bit key (RFC 7518 §3.2); refuse to start with a weak one.
        if len(value) < MIN_SECRET_LENGTH:
            raise ValueError(f"must be at least {MIN_SECRET_LENGTH} characters")
        return value

    @model_validator(mode="after")
    def encryption_key_present(self):
        if not self.credential_keyring:
            raise ValueError("credential_encryption_key or credential_encryption_keys is required")
        return self

    @property
    def credential_keyring(self) -> str:
        return self.credential_encryption_keys.strip() or self.credential_encryption_key.strip()

    @property
    def uses_dev_secrets(self) -> bool:
        return self.session_secret == DEV_SESSION_SECRET or self.chat_token_secret == DEV_CHAT_SECRET

    @property
    def seed_server_list(self) -> list[SeedServer]:
        return [SeedServer(**s) for s in json.loads(self.seed_servers)]

    @property
    def allowed_internal_host_patterns(self) -> list[str]:
        return _split(self.allowed_internal_hosts)

    @property
    def allowed_mcp_host_list(self) -> list[str]:
        return _split(self.allowed_mcp_hosts)

    @property
    def hosted_mcp_allowed_registry_list(self) -> list[str]:
        return _split(self.hosted_mcp_allowed_registries)


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


PLACEHOLDER_PREFIX = "REPLACE_WITH"


def llm_configured(settings: Settings) -> bool:
    key = settings.llm_api_key.strip()
    return bool(key) and not key.startswith(PLACEHOLDER_PREFIX)
