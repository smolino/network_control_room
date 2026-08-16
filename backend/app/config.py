import os


class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
    # Only Postgres+PostGIS supports the Router.location geography column and
    # the nearest-router spatial queries built on it (see app/models.py and
    # api/routers.py) - SQLite/MySQL still work for everything else.
    is_postgres: bool = database_url.startswith("postgres")
    trap_host: str = os.getenv("TRAP_HOST", "0.0.0.0")
    trap_port: int = int(os.getenv("TRAP_PORT", "1162"))
    trap_community: str = os.getenv("TRAP_COMMUNITY", "public")

    # Sliding window flap detection
    flap_window_seconds: int = int(os.getenv("FLAP_WINDOW_SECONDS", "600"))
    flap_transition_threshold: int = int(os.getenv("FLAP_TRANSITION_THRESHOLD", "4"))

    cors_origins: list[str] = os.getenv("CORS_ORIGINS", "*").split(",")

    # Outbound email for the Human Review "send to SOC/maintenance" action.
    # If smtp_host is unset, notifications are recorded for audit purposes
    # but marked "simulated" rather than actually delivered.
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_use_tls: bool = os.getenv("SMTP_USE_TLS", "true").lower() not in ("false", "0", "")
    smtp_from: str = os.getenv("SMTP_FROM", "noc@network-control-room.local")


settings = Settings()
