from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional convenience dependency
    load_dotenv = None

try:
    import bcrypt
except ImportError as exc:  # pragma: no cover - dependency guard
    bcrypt = None
    BCRYPT_IMPORT_ERROR = exc
else:
    BCRYPT_IMPORT_ERROR = None

try:
    from sqlalchemy import create_engine, event, select
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import sessionmaker
except ImportError as exc:  # pragma: no cover - dependency guard
    create_engine = None
    event = None
    select = None
    sessionmaker = None
    SQLAlchemyError = Exception
    SQLALCHEMY_IMPORT_ERROR = exc
else:
    SQLALCHEMY_IMPORT_ERROR = None


BASE_DIR = Path(__file__).resolve().parent
if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env", override=False)


DEFAULT_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/bamboo_ai.db")


@dataclass
class Runtime:
    database_url: str
    engine: object
    session_factory: object
    models: object


def fail(message: str, exit_code: int = 1) -> int:
    print(message, file=sys.stderr)
    return exit_code


def ensure_runtime_dependencies() -> None:
    missing = []
    if SQLALCHEMY_IMPORT_ERROR is not None:
        missing.append("sqlalchemy")
    if BCRYPT_IMPORT_ERROR is not None:
        missing.append("bcrypt")

    if missing:
        package_list = " ".join(missing)
        raise SystemExit(
            fail(
                f"Missing required package(s): {', '.join(missing)}. "
                f"Install them with `pip install {package_list}`."
            )
        )


def ensure_sqlite_parent_dir(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return

    raw_path = database_url.removeprefix("sqlite:///")
    if raw_path in {"", ":memory:"} or raw_path.startswith("file:"):
        return

    db_path = Path(raw_path)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)


def load_models():
    from models import Base, User, UserRole, UserStatus

    return {
        "Base": Base,
        "User": User,
        "UserRole": UserRole,
        "UserStatus": UserStatus,
    }


def build_engine(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, future=True, connect_args=connect_args)

    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def bootstrap_runtime(database_url: str) -> Runtime:
    ensure_runtime_dependencies()
    ensure_sqlite_parent_dir(database_url)
    models = load_models()
    engine = build_engine(database_url)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return Runtime(
        database_url=database_url,
        engine=engine,
        session_factory=factory,
        models=models,
    )


def initialize_database(runtime: Runtime) -> None:
    runtime.models["Base"].metadata.create_all(runtime.engine)


def hash_password(raw_password: str) -> str:
    if not raw_password:
        raise ValueError("Password cannot be empty.")
    return bcrypt.hashpw(raw_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def reset_admin(runtime: Runtime, username: str, password: str, display_name: str | None = None) -> int:
    initialize_database(runtime)

    user_model = runtime.models["User"]
    user_role = runtime.models["UserRole"]
    user_status = runtime.models["UserStatus"]
    password_hash = hash_password(password)

    with runtime.session_factory() as session:
        user = session.execute(
            select(user_model).where(user_model.username == username)
        ).scalar_one_or_none()

        created = user is None
        if created:
            user = user_model(
                username=username,
                display_name=display_name or username,
                password_hash=password_hash,
                role=user_role.SUPER_ADMIN,
                status=user_status.ACTIVE,
                is_active=True,
            )
            session.add(user)
        else:
            user.password_hash = password_hash
            user.role = user_role.SUPER_ADMIN
            user.status = user_status.ACTIVE
            user.is_active = True
            if display_name:
                user.display_name = display_name

        session.commit()
        session.refresh(user)

    action = "created" if created else "reset"
    print(
        f"Super admin {action}: username={user.username}, role={user.role.value}, "
        f"database={runtime.database_url}"
    )
    return 0


def handle_init_db(args: argparse.Namespace) -> int:
    runtime = bootstrap_runtime(args.database_url)
    initialize_database(runtime)
    print(f"Database initialized: {runtime.database_url}")
    return 0


def handle_reset_admin(args: argparse.Namespace) -> int:
    runtime = bootstrap_runtime(args.database_url)
    return reset_admin(
        runtime=runtime,
        username=args.username,
        password=args.password,
        display_name=args.display_name,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Management commands for Bamboo AI.")
    parser.add_argument(
        "--database-url",
        default=DEFAULT_DATABASE_URL,
        help="SQLAlchemy database URL. Defaults to DATABASE_URL or sqlite:///./data/bamboo_ai.db",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db_parser = subparsers.add_parser(
        "init-db",
        help="Create database tables if they do not exist.",
    )
    init_db_parser.set_defaults(handler=handle_init_db)

    reset_admin_parser = subparsers.add_parser(
        "reset-admin",
        help="Create or reset the super admin account.",
    )
    reset_admin_parser.add_argument("--username", required=True, help="Admin username.")
    reset_admin_parser.add_argument("--password", required=True, help="Admin password.")
    reset_admin_parser.add_argument(
        "--display-name",
        help="Optional display name. Defaults to the username when creating a new admin.",
    )
    reset_admin_parser.set_defaults(handler=handle_reset_admin)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return args.handler(args)
    except ValueError as exc:
        return fail(str(exc))
    except SQLAlchemyError as exc:
        return fail(f"Database operation failed: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
