"""ECS 스케줄 / 로컬: python -m app.agent"""

from app.agent.runner import run_agent
from app.db import SessionLocal


def main() -> None:
    db = SessionLocal()
    try:
        result = run_agent(db)
        print(result)
    finally:
        db.close()


if __name__ == "__main__":
    main()
