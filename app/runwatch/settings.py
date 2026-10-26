"""Application settings, read from environment variables.

Every setting here is listed in `.env.example`.
"""

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsError(RuntimeError):
    """Raised when the environment does not provide valid settings."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # SecretStr keeps the URL (and its password) out of reprs and logs.
    database_url: SecretStr
    db_connect_timeout_s: int = Field(default=2, ge=1)


def load_settings() -> Settings:
    """Build `Settings` from the environment, naming the bad variables on failure.

    The error message names environment variables only, never their values.
    """
    try:
        return Settings()
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_input=False, include_url=False):
            name = ".".join(str(part) for part in error["loc"]).upper()
            problems.append(f"{name}: {error['msg']}")
        raise SettingsError(
            "Invalid environment settings: " + "; ".join(problems)
        ) from None
