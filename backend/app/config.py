from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

_DEFAULT_SQLITE = f"sqlite:///{(Path(__file__).resolve().parent.parent / 'jinan_travel.db').as_posix()}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Unset preserves the deployed application's existing read/write behaviour.
    # Development and diagnostic commands opt into an explicit guarded mode.
    app_db_mode: Literal["local", "production_readonly"] | None = None
    # Docker 없을 때는 기본 SQLite. Postgres 쓰려면 DATABASE_URL 환경변수 설정.
    database_url: str = _DEFAULT_SQLITE
    jwt_secret: str = "change-me-in-production-jinan-travel-2026"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,https://localhost:5173,https://127.0.0.1:5173"

    aws_region: str = "ap-northeast-2"
    s3_bucket: str = ""
    s3_public_base_url: str = ""  # CloudFront 또는 S3 website URL

    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    # This project's allow-list currently enables 120B; chat falls back to groq_model if overridden model is blocked.
    groq_chat_model: str = "openai/gpt-oss-120b"
    # 값이 있으면 ArcGIS를 forStorage=true로 호출해 선택 좌표를 영구 저장할 수 있다.
    # 값이 없으면 UI에서 참고 위치로만 노출하고 지도 직접 지정 절차를 거친다.
    arcgis_api_key: str = ""
    # Optional structured POI discovery. Brave Place results are discovery-only
    # unless the account explicitly grants storage rights; transient IDs are not
    # persisted as durable coordinate identifiers.
    brave_search_api_key: str = ""
    brave_place_enabled: bool = True
    brave_search_storage_rights: bool = False
    # Safety ceiling only. The runner stops earlier on completed outcomes or
    # repeated no-progress observations rather than consuming a fixed quota.
    agent_max_steps: int = 180
    agent_autonomous_research: bool = False
    agent_allow_auto_create: bool = False
    agent_allow_auto_merge: bool = False
    # 쉼표 구분. 기본: 성주한
    admin_emails: str = "joohan92@naver.com"

    @model_validator(mode="after")
    def validate_database_mode(self) -> "Settings":
        """Fail closed when an explicit safety mode points at the wrong DB."""

        try:
            url = make_url(self.database_url)
            backend = url.get_backend_name()
        except Exception as exc:
            raise ValueError("DATABASE_URL is not a valid SQLAlchemy URL") from exc

        if self.app_db_mode == "production_readonly":
            if backend not in {"postgresql", "postgres"}:
                raise ValueError(
                    "APP_DB_MODE=production_readonly requires a PostgreSQL DATABASE_URL"
                )
            return self

        if self.app_db_mode != "local" or backend == "sqlite":
            return self

        if backend not in {"postgresql", "postgres"}:
            raise ValueError("APP_DB_MODE=local supports only SQLite or PostgreSQL")
        host = (url.host or "").strip().lower()
        if host not in {"localhost", "127.0.0.1", "::1", "db"}:
            raise ValueError("APP_DB_MODE=local refuses a non-local PostgreSQL host")
        expected_port = 5432 if host == "db" else 55432
        if url.port != expected_port:
            raise ValueError(
                f"APP_DB_MODE=local requires PostgreSQL port {expected_port} for host {host}"
            )
        if (url.database or "").strip().lower() != "cloudmiddle_local":
            raise ValueError(
                "APP_DB_MODE=local requires the PostgreSQL database cloudmiddle_local"
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.admin_emails.split(",") if e.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production_readonly(self) -> bool:
        return self.app_db_mode == "production_readonly"

    @property
    def runtime_db_mode(self) -> str:
        # "application" is the backward-compatible deployed web/worker mode.
        # It is deliberately not accepted as an APP_DB_MODE environment value.
        return self.app_db_mode or "application"


settings = Settings()
