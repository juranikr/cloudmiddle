from sqlalchemy.orm import Session

from app.auth import hash_password
from app.models import User

SEED_USERS = [
    {"email": "alice@test.com", "display_name": "앨리스", "password": "test1234"},
    {"email": "bob@test.com", "display_name": "밥", "password": "test1234"},
    {"email": "carol@test.com", "display_name": "캐롤", "password": "test1234"},
]


def seed_data(db: Session) -> None:
    if db.query(User).count() > 0:
        return

    for item in SEED_USERS:
        db.add(
            User(
                email=item["email"],
                display_name=item["display_name"],
                password_hash=hash_password(item["password"]),
            )
        )
    db.commit()
