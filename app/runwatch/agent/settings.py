"""Agent settings, read from environment variables (listed in `.env.example`).

The agent has its own settings class: it needs no database.
"""

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from runwatch.settings import SettingsError

DEFAULT_MODEL = "claude-opus-5-5"


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # SecretStr keeps both secrets out of reprs and logs.
    agent_webhook_token: SecretStr
    agent_incidents_dir: str = "incidents"
    agent_cooldown_s: float = Field(default=600, ge=0)
    agent_model: str = DEFAULT_MODEL
    agent_model_timeout_s: float = Field(default=60, gt=0)
    agent_api_retry_delay_s: float = Field(default=2, gt=0)
    agent_verify_interval_s: float = Field(default=30, gt=0)
    agent_verify_timeout_s: float = Field(default=600, gt=0)
    anthropic_api_key: SecretStr | None = None
    # Only used to redact incident reports; nothing posts to GitHub yet.
    github_token: SecretStr | None = None

    @field_validator("agent_webhook_token")
    @classmethod
    def _token_not_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("must not be empty")
        return value

    @property
    def secret_values(self) -> list[str]:
        """Values the incident report must never contain."""
        secrets = [self.agent_webhook_token, self.anthropic_api_key, self.github_token]
        return [s.get_secret_value() for s in secrets if s is not None]

    @property
    def has_api_key(self) -> bool:
        key = self.anthropic_api_key
        return key is not None and bool(key.get_secret_value().strip())


def load_agent_settings() -> AgentSettings:
    """Build `AgentSettings`, raising `SettingsError` that names variables only."""
    try:
        return AgentSettings()
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_input=False, include_url=False):
            name = ".".join(str(part) for part in error["loc"]).upper()
            problems.append(f"{name}: {error['msg']}")
        raise SettingsError(
            "Invalid environment settings: " + "; ".join(problems)
        ) from None
