"""
app/core/config.py

Central configuration using pydantic-settings.
All settings are loaded from environment variables / .env file.
"""
from functools import lru_cache
from typing import List

from pydantic import AnyHttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    APP_ENV: str = "development"
    APP_NAME: str = "Loan Servicing Platform"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # --- API ---
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_PREFIX: str = "/api/v1"
    ALLOWED_ORIGINS: List[str] = ["http://localhost:3000"]

    @field_validator("ALLOWED_ORIGINS", mode="before")
    @classmethod
    def parse_origins(cls, v):
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v

    # --- Security ---
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    ALGORITHM: str = "HS256"

    # --- Database ---
    DATABASE_URL: str
    DATABASE_SUPERUSER_URL: str = ""
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 40
    DATABASE_POOL_TIMEOUT: int = 30

    # --- Redis / Celery ---
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # --- S3 ---
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "us-east-1"
    S3_BUCKET_DOCUMENTS: str = "loan-servicing-documents-dev"
    S3_BUCKET_EXPORTS: str = "loan-servicing-exports-dev"

    # --- Investran ---
    INVESTRAN_SFTP_HOST: str = ""
    INVESTRAN_SFTP_PORT: int = 22
    INVESTRAN_SFTP_USER: str = ""
    INVESTRAN_SFTP_PASSWORD: str = ""
    INVESTRAN_SFTP_REMOTE_PATH: str = "/outbound"

    # --- Email ---
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    EMAIL_FROM: str = "servicing@example.com"
    EMAIL_FROM_NAME: str = "Loan Servicing"

    # --- Sentry ---
    SENTRY_DSN: str = ""

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings instance. Use as a FastAPI dependency:
        settings: Settings = Depends(get_settings)
    Or import directly for non-request contexts:
        from app.core.config import settings
    """
    return Settings()


# Module-level singleton for non-DI usage
settings = get_settings()
