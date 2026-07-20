from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_SQLITE = f"sqlite:///{(Path(__file__).resolve().parent.parent / 'jinan_travel.db').as_posix()}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Docker 없을 때는 기본 SQLite. Postgres 쓰려면 DATABASE_URL 환경변수 설정.
    database_url: str = _DEFAULT_SQLITE
    jwt_secret: str = "change-me-in-production-jinan-travel-2026"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,https://localhost:5173,https://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


settings = Settings()
