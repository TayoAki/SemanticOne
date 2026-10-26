"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:///./data/semanticone.db")
    # Heroku/Railway style URLs use the legacy scheme; SQLAlchemy wants an explicit driver.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


@dataclass(frozen=True)
class Settings:
    app_name: str = "SemanticOne"
    environment: str = field(default_factory=lambda: os.environ.get("ENVIRONMENT", "development"))
    base_url: str = field(default_factory=lambda: os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/"))
    secret_key: str = field(default_factory=lambda: os.environ.get("SECRET_KEY", ""))
    encryption_key: str = field(default_factory=lambda: os.environ.get("ENCRYPTION_KEY", ""))
    database_url: str = field(default_factory=_database_url)
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "./data")))

    # Warehouse connections
    allow_private_networks: bool = field(default_factory=lambda: _bool("ALLOW_PRIVATE_NETWORKS"))
    query_timeout_seconds: int = field(default_factory=lambda: int(os.environ.get("QUERY_TIMEOUT_SECONDS", "60")))

    # Stripe billing (optional; billing UI is disabled when unset)
    stripe_secret_key: str = field(default_factory=lambda: os.environ.get("STRIPE_SECRET_KEY", ""))
    stripe_webhook_secret: str = field(default_factory=lambda: os.environ.get("STRIPE_WEBHOOK_SECRET", ""))
    stripe_price_pro: str = field(default_factory=lambda: os.environ.get("STRIPE_PRICE_PRO", ""))
    stripe_price_business: str = field(default_factory=lambda: os.environ.get("STRIPE_PRICE_BUSINESS", ""))

    # Outbound email (optional; links are logged when unset)
    smtp_host: str = field(default_factory=lambda: os.environ.get("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("SMTP_PORT", "587")))
    smtp_username: str = field(default_factory=lambda: os.environ.get("SMTP_USERNAME", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.environ.get("SMTP_FROM", "SemanticOne <no-reply@localhost>"))

    sales_email: str = field(default_factory=lambda: os.environ.get("SALES_EMAIL", "sales@example.com"))
    support_email: str = field(default_factory=lambda: os.environ.get("SUPPORT_EMAIL", "support@example.com"))
    legal_entity: str = field(default_factory=lambda: os.environ.get("LEGAL_ENTITY", "SemanticOne"))
    signups_enabled: bool = field(default_factory=lambda: _bool("SIGNUPS_ENABLED", True))

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def billing_enabled(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_price_pro)

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")


INSTANCE_KEYS_FILE = "instance_keys.json"


def _instance_keys(data_dir: Path) -> dict[str, str]:
    """Load, or create on first boot, keys kept in DATA_DIR (0600) when env vars don't provide them.

    Keeping the encryption key on the app's volume rather than in the database means a
    database dump alone cannot decrypt stored warehouse credentials. Multi-replica
    deployments should set SECRET_KEY and ENCRYPTION_KEY explicitly instead.
    """
    from cryptography.fernet import Fernet

    path = data_dir / INSTANCE_KEYS_FILE
    if not path.exists():
        keys = {"secret_key": secrets.token_urlsafe(48), "encryption_key": Fernet.generate_key().decode()}
        tmp = data_dir / f".{INSTANCE_KEYS_FILE}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(keys, handle)
        try:
            os.link(tmp, path)  # atomic create-if-absent: concurrent workers agree on one file
        except FileExistsError:
            pass
        finally:
            os.unlink(tmp)
        logging.basicConfig(level=logging.INFO, stream=sys.stdout)
        logging.getLogger("semanticone").warning(
            "Generated SECRET_KEY/ENCRYPTION_KEY in %s. Back up this file: losing it makes saved "
            "warehouse credentials unreadable.",
            path,
        )
    return json.loads(path.read_text())


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if not settings.secret_key or not settings.encryption_key:
        keys = _instance_keys(settings.data_dir)
        if not settings.secret_key:
            object.__setattr__(settings, "secret_key", keys["secret_key"])
        if not settings.encryption_key:
            object.__setattr__(settings, "encryption_key", keys["encryption_key"])
    return settings
