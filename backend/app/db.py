import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

# SQLite needs its data directory to exist and a special connect arg for
# multi-threaded access (the API and the trap listener share one process).
connect_args = {}
pool_kwargs = {}
if settings.database_url.startswith("sqlite"):
    os.makedirs("./data", exist_ok=True)
    connect_args = {"check_same_thread": False}
elif settings.is_postgres:
    # The trap listener processes traps for different routers concurrently
    # (see snmp/trap_listener.py) - default pool_size=5/max_overflow=10
    # would just move the bottleneck from "one at a time on the event
    # loop" to "one at a time waiting for a free connection".
    pool_kwargs = {"pool_size": 20, "max_overflow": 20}

engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True, **pool_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=True, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from app import models  # noqa: F401 - ensure models are registered

    if settings.is_postgres:
        # Covers a Postgres instance provisioned without the extension
        # pre-created - the official postgis/postgis image already has it,
        # but this keeps init_db() correct against any Postgres+PostGIS
        # server. Must run before create_all since Router.location uses it.
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))

    Base.metadata.create_all(bind=engine)
