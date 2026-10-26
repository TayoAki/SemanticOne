"""ORM models for the multi-tenant application."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    memberships: Mapped[list[Membership]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    plan: Mapped[str] = mapped_column(String(32), default="free")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subscription_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    memberships: Mapped[list[Membership]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    semantic_models: Mapped[list[SemanticModel]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    connections: Mapped[list[Connection]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="organization", cascade="all, delete-orphan")


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "organization_id", name="uq_membership_user_org"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), default=ROLE_MEMBER)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="memberships")
    organization: Mapped[Organization] = relationship(back_populates="memberships")

    @property
    def is_admin(self) -> bool:
        return self.role in (ROLE_OWNER, ROLE_ADMIN)


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_MEMBER)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    organization: Mapped[Organization] = relationship()


class Connection(Base):
    """A warehouse connection. Credentials are stored encrypted."""

    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_connection_org_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(32))
    encrypted_url: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    organization: Mapped[Organization] = relationship(back_populates="connections")


class SemanticModel(Base):
    __tablename__ = "semantic_models"
    __table_args__ = (UniqueConstraint("organization_id", "slug", name="uq_model_org_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("connections.id", ondelete="SET NULL"), nullable=True
    )
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    organization: Mapped[Organization] = relationship(back_populates="semantic_models")
    connection: Mapped[Connection | None] = relationship()
    versions: Mapped[list[ModelVersion]] = relationship(
        back_populates="semantic_model", cascade="all, delete-orphan", order_by="ModelVersion.version"
    )


class ModelVersion(Base):
    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("semantic_model_id", "version", name="uq_model_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    semantic_model_id: Mapped[int] = mapped_column(ForeignKey("semantic_models.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(500), default="")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    semantic_model: Mapped[SemanticModel] = relationship(back_populates="versions")
    created_by: Mapped[User | None] = relationship()


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(24))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization] = relationship(back_populates="api_keys")


class QueryLog(Base):
    """One row per executed query; drives usage metering and quotas."""

    __tablename__ = "query_logs"
    __table_args__ = (Index("ix_query_logs_org_created", "organization_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    semantic_model_id: Mapped[int | None] = mapped_column(
        ForeignKey("semantic_models.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    api_key_id: Mapped[int | None] = mapped_column(ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="web")  # web | api | mcp
    request: Mapped[str] = mapped_column(Text, default="")
    sql: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | error
    error: Mapped[str] = mapped_column(Text, default="")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlatformSetting(Base):
    """Operator-level settings changed from the admin console (e.g. Stripe connection)."""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


LEAD_STATUSES = ("new", "contacted", "won", "lost")


class SalesLead(Base):
    __tablename__ = "sales_leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(320))
    company: Mapped[str] = mapped_column(String(200), default="")
    plan_interest: Mapped[str] = mapped_column(String(32), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    organization: Mapped[Organization | None] = relationship()
