from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def create_database_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(
        database_url, connect_args=connect_args, pool_pre_ping=True, hide_parameters=True
    )
    if database_url.startswith("sqlite"):
        database_name = str(engine.url.database or "")
        use_wal = database_name not in {"", ":memory:"} and "mode=memory" not in database_url

        @event.listens_for(engine, "connect")
        def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                if use_wal:
                    cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db(request: Request) -> Generator[Session, None, None]:
    """Yield a session from the factory owned by this FastAPI application."""

    session_factory: sessionmaker[Session] = request.app.state.database_session_factory
    with session_factory() as session:
        yield session
