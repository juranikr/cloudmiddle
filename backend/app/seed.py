"""앱 기동 시 계정 동기화.

운영 비밀번호는 환경변수(ECS Secrets)로만 주입합니다. Git에 평문 비밀번호를 두지 마세요.
- SEED_PASSWORD_JOOHAN
- SEED_PASSWORD_GUKSEO
테스트 계정만 코드에 기본 비밀번호(test1234)를 둡니다.
"""

from __future__ import annotations

import os

from sqlalchemy.orm import Session

from app.auth import hash_password
from app.models import User

# aliases: 기존 이메일도 찾아 같은 user_id(마커 소유)를 유지한 채 갱신
ACCOUNT_SPECS = [
    {
        "aliases": ["alice@test.com", "joohan92@naver.com"],
        "email": "joohan92@naver.com",
        "display_name": "성주한",
        "password_env": "SEED_PASSWORD_JOOHAN",
    },
    {
        "aliases": ["bob@test.com", "tjwjd629@naver.com"],
        "email": "tjwjd629@naver.com",
        "display_name": "국서정",
        "password_env": "SEED_PASSWORD_GUKSEO",
    },
    {
        "aliases": ["carol@test.com", "test@test.com"],
        "email": "test@test.com",
        "display_name": "테스트",
        "password_env": "SEED_PASSWORD_TEST",
        "default_password": "test1234",
    },
]


def seed_data(db: Session) -> None:
    changed = False
    for spec in ACCOUNT_SPECS:
        user = None
        for alias in spec["aliases"]:
            user = db.query(User).filter(User.email == alias).first()
            if user is not None:
                break

        password = os.environ.get(spec["password_env"] or "", "").strip()
        if not password:
            password = (spec.get("default_password") or "").strip()

        if user is None:
            if not password:
                # 운영에서 비밀이 아직 없으면 해당 계정은 건너뜀
                continue
            db.add(
                User(
                    email=spec["email"],
                    display_name=spec["display_name"],
                    password_hash=hash_password(password),
                )
            )
            changed = True
            continue

        if user.email != spec["email"]:
            user.email = spec["email"]
            changed = True
        if user.display_name != spec["display_name"]:
            user.display_name = spec["display_name"]
            changed = True
        if password:
            user.password_hash = hash_password(password)
            changed = True

    if changed:
        db.commit()
