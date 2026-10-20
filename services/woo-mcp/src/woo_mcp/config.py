from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Runtime configuration from environment variables; credentials come from K8s Secrets."""

    woo_base_url: str = "http://wordpress"
    woo_store_host: str
    woo_public_url: str = ""
    woo_consumer_key: str
    woo_consumer_secret: str
    woo_timeout_seconds: float = 15.0
    mcp_auth_token: str
    app_version: str = "dev"
    allowed_hosts: str = "localhost:*,127.0.0.1:*"
    port: int = 8000

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]
