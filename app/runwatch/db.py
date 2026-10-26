"""Database engine, session factory and the `get_session` dependency.

This module holds the engine and session only; models live elsewhere.
"""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from runwatch.settings import Settings


def make_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url.get_secret_value(),
        connect_args={"connect_timeout": settings.db_connect_timeout_s},
        pool_pre_ping=True,
    )


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_session(request: Request) -> Iterator[Session]:
    """Yield a session bound to the app's engine.

    Routes commit explicitly. Anything left uncommitted is rolled back, and the
    session is closed when the request ends, also when the route raises.
    """
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        try:
            session.rollback()
        finally:
            session.close()
