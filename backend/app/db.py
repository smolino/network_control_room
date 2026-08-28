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
    _seed_default_router_models()


# Starter catalog for the Add Fleet form's Vendor/Model dropdowns (see
# app.models.RouterModel) - a realistic multi-vendor mix (Cisco IP/MPLS core
# + its ONS 15454 optical platform, Arista, Ciena) plus the CPE-class models
# already used elsewhere in this project (simulator/generate_customer_routers.py,
# frontend/src/components/AddFleet.jsx's old placeholders), so the dropdowns
# aren't empty on a fresh install.
DEFAULT_ROUTER_MODELS = [
    ("Cisco", "ASR1001-X"),
    ("Cisco", "ASR1002-HX"),
    ("Cisco", "ASR9001"),
    ("Cisco", "ASR9010"),
    ("Cisco", "ASR9903"),
    ("Cisco", "8201"),
    ("Cisco", "8202"),
    ("Cisco", "8712"),
    ("Cisco", "ONS 15454"),
    ("Cisco", "ISR4451-X"),
    ("Cisco", "ISR4331"),
    ("Cisco", "ISR4321"),
    ("Cisco", "Catalyst 8300-1N1S"),
    ("Cisco", "Catalyst 8500L-8S4X"),
    ("Cisco", "CRS-1000"),
    ("Cisco", "NCS 5501"),
    ("Cisco", "ISR1100-4G"),
    ("Cisco", "ISR1101"),
    ("Cisco", "C1111-8P"),
    ("Cisco", "Catalyst 1300"),
    ("Cisco", "RV340"),
    ("Cisco", "RV160"),
    ("Arista", "7280R3"),
    ("Arista", "7280SR3"),
    ("Arista", "7500R3"),
    ("Arista", "7050X3"),
    ("Ciena", "6500"),
    ("Ciena", "5170"),
    ("Ciena", "8180"),
    ("Ciena", "WaveLogic 5"),
]


def _seed_default_router_models() -> None:
    from app.models import RouterModel

    db = SessionLocal()
    try:
        if db.query(RouterModel).first() is not None:
            return
        db.bulk_save_objects([RouterModel(vendor=vendor, model=model) for vendor, model in DEFAULT_ROUTER_MODELS])
        db.commit()
    finally:
        db.close()
