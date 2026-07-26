"""ECS 스케줄 / 로컬: python -m app.agent"""

from app.agent.runner import run_agent
from app.db import Base, SessionLocal, engine


def main() -> None:
    # API 배포와 무관하게 새 테이블(검색/방문 로그 등)이 있도록 보장
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        result = run_agent(db)
        print(result)
    finally:
        db.close()


if __name__ == "__main__":
    main()
