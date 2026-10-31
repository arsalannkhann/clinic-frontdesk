import ipaddress
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


def _https_or_loopback(value: str | None, name: str) -> str | None:
    if value is None or value == "":
        return None
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{name} must not carry a username or password")
    if parts.scheme == "https" and host:
        return value
    if parts.scheme == "http" and host in LOOPBACK_HOSTS:
        return value  # Google allows plain-http loopback redirect URIs for local development.
    raise ValueError(f"{name} must be https (http only for localhost)")


def _origin(value: str) -> str:
    """One browser origin (scheme://host[:port]) in canonical lowercase form, or ValueError.
    No wildcard, path, query, fragment or credentials: CORS compares origins exactly."""
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        raise ValueError("dashboard_origins must list origins") from None
    if (
        "*" in value
        or not parts.hostname
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username is not None
    ):
        raise ValueError("dashboard_origins must list origins such as https://dash.example")
    _https_or_loopback(value, "dashboard_origins")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"  # IPv6 literal
    default = {"https": 443, "http": 80}[parts.scheme]
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr
    # Only "development" relaxes development-only safety checks (e.g. worker --calendar fake).
    app_env: str = "production"
    # "kid1:<base64 32 bytes>,kid2:..." - the first key encrypts; all keys decrypt.
    token_encryption_keys: SecretStr | None = None
    # base64 32 bytes; HMAC key for crypto.phone_hash. Required by the Bolna gateway, which
    # hashes the provider-asserted caller number of each call session (503 when unset).
    phone_hash_key: SecretStr | None = None
    # Retention (T7, `clinic-voice retention purge`; nothing runs it automatically). A patient
    # record is kept until its latest appointment start plus this many days.
    patient_retention_days: int = Field(default=365, ge=1, le=3650)
    # Call sessions (encrypted caller number, verification state) older than this many days.
    call_session_retention_days: int = Field(default=90, ge=1, le=3650)
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_redirect_uri: str | None = None
    # Bolna platform API (providers/bolna.py). Unused by the gateway routes, which Bolna calls.
    bolna_api_key: SecretStr | None = None
    bolna_api_base: str = "https://api.bolna.ai"
    # Public base URL of this API as Bolna reaches it; the agent compiler builds tool URLs
    # from it (e.g. https://voice.clinic.example). Unset: compile-agent needs --gateway-url.
    bolna_gateway_base_url: str | None = None
    # Bolna execution webhooks (T8, POST /v1/webhooks/bolna/{token}).
    # Largest accepted body; larger requests get 413 without being read further.
    webhook_max_body_bytes: int = Field(default=65536, ge=1024, le=1048576)
    # Optional allowlist (comma-separated IPs or CIDR networks), defense in depth only: the
    # webhook credential is the authentication. Unset: any source address.
    bolna_webhook_allowed_ips: str | None = None
    # Number of trusted reverse proxies in front of the API. 0: the socket peer is the
    # client and X-Forwarded-For is ignored. n: the n-th address from the right of
    # X-Forwarded-For is the client (the address the outermost trusted proxy saw).
    trusted_proxy_hops: int = Field(default=0, ge=0, le=5)
    # Reconciliation: a session without a final provider status is polled through
    # GET /executions/{id} this long after its start or last provider update (doubling
    # after each poll, at most 24 h apart), at most this many sessions per housekeeping pass.
    bolna_reconcile_after_minutes: int = Field(default=30, ge=1, le=1440)
    bolna_reconcile_batch: int = Field(default=50, ge=1, le=1000)
    # Staff dashboard (T11): comma-separated browser origins allowed to call /v1/staff/* and
    # /v1/admin/* cross-origin (e.g. https://dash.clinic.example). Empty: no CORS headers at
    # all (serve the dashboard same-origin, or use the Vite dev-server proxy).
    dashboard_origins: str | None = None

    @field_validator("dashboard_origins")
    @classmethod
    def _dashboard_origins(cls, value):
        if value is None or not value.strip():
            return None
        origins = [_origin(part.strip()) for part in value.split(",") if part.strip()]
        return ",".join(dict.fromkeys(origins))

    def dashboard_origin_list(self) -> list[str]:
        return self.dashboard_origins.split(",") if self.dashboard_origins else []

    @field_validator("google_redirect_uri")
    @classmethod
    def _redirect(cls, value):
        return _https_or_loopback(value, "google_redirect_uri")

    @field_validator("bolna_api_base")
    @classmethod
    def _bolna_api(cls, value):
        return _https_or_loopback(value, "bolna_api_base")

    @field_validator("bolna_gateway_base_url")
    @classmethod
    def _gateway(cls, value):
        return _https_or_loopback(value, "bolna_gateway_base_url")

    @field_validator("bolna_webhook_allowed_ips")
    @classmethod
    def _allowed_ips(cls, value):
        if value is None or not value.strip():
            return None
        parts = [part.strip() for part in value.split(",") if part.strip()]
        try:
            for part in parts:
                ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise ValueError(
                "bolna_webhook_allowed_ips must list IP addresses or networks"
            ) from None
        return ",".join(parts)

    def webhook_networks(self):
        """The allowlist as ip_network objects, or None when unset (allow any)."""
        if not self.bolna_webhook_allowed_ips:
            return None
        return [
            ipaddress.ip_network(part, strict=False)
            for part in self.bolna_webhook_allowed_ips.split(",")
        ]

    def google_configured(self) -> bool:
        return bool(
            self.google_client_id
            and self.google_client_secret
            and self.google_client_secret.get_secret_value()
            and self.google_redirect_uri
            and self.token_encryption_keys
            and self.token_encryption_keys.get_secret_value()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
