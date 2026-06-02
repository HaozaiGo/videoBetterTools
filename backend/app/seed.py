from app.database import SessionLocal
from app.services import ensure_demo_user


def main() -> None:
    with SessionLocal() as db:
        ensure_demo_user(db)


if __name__ == "__main__":
    main()
