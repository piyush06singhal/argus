"""ARGUS Core Configuration."""
from __future__ import annotations

from typing import Any, List, Optional
from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    APP_NAME: str = "ARGUS"
    APP_VERSION: str = "0.1.0"
    APP_DESCRIPTION: str = "Autonomous Software Reliability & Engineering Intelligence"
    API_ENVIRONMENT: str = "development"
    API_DEBUG: bool = False

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://argus:argus_password@localhost:5432/argus_db"
    DATABASE_HOST: str = "localhost"
    DATABASE_PORT: int = 5432
    DATABASE_NAME: str = "argus_db"
    DATABASE_USER: str = "argus"
    DATABASE_PASSWORD: str = "argus_password"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379

    # API
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_WORKERS: int = 4
    CORS_ORIGINS: List[str] = ["http://localhost:3000"]

    # Authentication (future use)
    AUTH_SECRET: str = "change-this-in-production"
    AUTH_ALGORITHM: str = "HS256"
    AUTH_TOKEN_EXPIRY: int = 3600

    # AI Provider (future use)
    AI_PROVIDER: str = "mock"
    AI_API_KEY: Optional[str] = None

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"

    # Ingestion
    INGESTION_BATCH_SIZE: int = 100
    INGESTION_FLUSH_INTERVAL: int = 5
    INGESTION_WORKER_ENABLED: bool = True
    # Payload limits (§27) — protected against oversized observability payloads.
    MAX_LOG_MESSAGE_LENGTH: int = 8192
    MAX_METADATA_LENGTH: int = 10000
    MAX_METRIC_LABELS: int = 32
    MAX_LABEL_KEY_LENGTH: int = 128
    MAX_LABEL_VALUE_LENGTH: int = 512

    # Data Retention (days)
    RETENTION_LOGS: int = 90
    RETENTION_METRICS: int = 90
    RETENTION_TRACES: int = 30
    RETENTION_EVENTS: int = 90

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            import json
            return json.loads(v)
        return v

    def model_post_init(self, __context: Any) -> None:
        """Derive REDIS_HOST/PORT from REDIS_URL unless explicitly configured.

        This keeps the Redis health check correct wherever the app runs
        (local, Docker Compose, cluster) as long as REDIS_URL is set.
        """
        if "REDIS_HOST" not in self.model_fields_set or "REDIS_PORT" not in self.model_fields_set:
            parsed = urlparse(self.REDIS_URL)
            if parsed.hostname:
                self.REDIS_HOST = parsed.hostname
            if parsed.port:
                self.REDIS_PORT = parsed.port

    @property
    def is_development(self) -> bool:
        return self.API_ENVIRONMENT == "development"

    @property
    def is_testing(self) -> bool:
        return self.API_ENVIRONMENT == "test"

    @property
    def is_production(self) -> bool:
        return self.API_ENVIRONMENT == "production"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


def get_settings() -> Settings:
    """Get application settings."""
    return Settings()
