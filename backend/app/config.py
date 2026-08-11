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
    # Safety ceiling only. The runner stops earlier on completed outcomes or
    # repeated no-progress observations rather than consuming a fixed quota.
    agent_max_steps: int = 180
    agent_autonomous_research: bool = False
    agent_allow_auto_create: bool = False
    agent_allow_auto_merge: bool = False
    # 쉼표 구분. 기본: 성주한
    admin_emails: str = "joohan92@naver.com"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.admin_emails.split(",") if e.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


settings = Settings()
