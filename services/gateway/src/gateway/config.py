import json
from functools import lru_cache

from pydantic import BaseModel
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


class Settings(BaseSettings):
    """Runtime configuration from environment variables; secrets come from K8s Secrets."""

    database_url: str
    credential_encryption_key: str
    session_secret: str = "dev-only-session-secret-change-me"
    chat_token_secret: str = "dev-only-chat-secret-change-me"
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    seed_servers: str = "[]"
    allowed_internal_hosts: str = ""
    allowed_mcp_hosts: str = "localhost:*,127.0.0.1:*"

    health_interval_seconds: float = 15.0
    health_timeout_seconds: float = 5.0
    unreachable_after_failures: int = 2
    max_concurrent_calls_per_upstream: int = 10
    default_call_timeout_seconds: int = 20

    @property
    def seed_server_list(self) -> list[SeedServer]:
        return [SeedServer(**s) for s in json.loads(self.seed_servers)]

    @property
    def allowed_internal_host_patterns(self) -> list[str]:
        return _split(self.allowed_internal_hosts)

    @property
    def allowed_mcp_host_list(self) -> list[str]:
        return _split(self.allowed_mcp_hosts)


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
