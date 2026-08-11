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
from app.models import AgentKnowledge, User

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

    starter_knowledge = [
        {
            "topic": "global:editorial_standard",
            "title": "여행 장소 정보 편집 기준",
            "scope": "global",
            "city_id": None,
            "content": (
                "장소 소개는 현재 위치의 의미 → 역사 사건/인물 → 지금 볼 것 → 방문 정보 순으로 연결한다. "
                "운영시간·가격처럼 바뀌는 정보는 확인일과 출처를 남기고, 위치 좌표는 WGS84와 원본 좌표계를 구분한다. "
                "단일 블로그 주장보다 공식 기관·박물관·교차 출처를 우선한다."
            ),
        },
        {
            "topic": "city:2:starter_shenyang",
            "title": "선양 초기 조사 지도",
            "scope": "city",
            "city_id": 2,
            "content": (
                "첫 조사 축: 沈阳故宫(청 초기 황궁), 张氏帅府(장작림·장학량), 九一八历史博物馆, "
                "北陵/昭陵과 东陵/福陵, 中街·西塔, 中国工业博物馆, 辽宁省博物馆. "
                "명소를 나열하지 말고 청 초기 수도 → 근대 군벌/동북 역사 → 9·18 → 공업도시라는 시간축과 "
                "현재 동선을 연결한다. 각 장소는 공식 출처를 포함한 승인 제안으로 만든다."
            ),
        },
    ]
    for item in starter_knowledge:
        if db.query(AgentKnowledge.id).filter(AgentKnowledge.topic == item["topic"]).first() is None:
            db.add(AgentKnowledge(**item))
            changed = True

    if changed:
        db.commit()
