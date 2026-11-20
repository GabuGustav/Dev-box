from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Principal(Base):
    __tablename__ = "principals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    api_keys: Mapped[list["APIKey"]] = relationship(back_populates="principal")
    workspaces: Mapped[list["Workspace"]] = relationship(back_populates="owner")


class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id", ondelete="CASCADE"))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    principal: Mapped[Principal] = relationship(back_populates="api_keys")


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("normalized_url", name="uq_repositories_normalized_url"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    normalized_url: Mapped[str] = mapped_column(String(500), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), default="git", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    workspaces: Mapped[list["Workspace"]] = relationship(back_populates="repository")


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        Index("ix_workspaces_owner_created", "owner_id", "created_at"),
        Index("ix_workspaces_repository", "repository_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    owner_id: Mapped[str] = mapped_column(ForeignKey("principals.id", ondelete="RESTRICT"))
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="RESTRICT"))
    branch: Mapped[str] = mapped_column(String(255), default="main", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="registered", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    owner: Mapped[Principal] = relationship(back_populates="workspaces")
    repository: Mapped[Repository] = relationship(back_populates="workspaces")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("principals.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str] = mapped_column(String(30), default="success", nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
