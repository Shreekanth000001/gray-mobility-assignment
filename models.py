"""
models.py
=========

Persistence layer for the Identity Reconciliation service.

Design notes
------------
* A single self-referential ``contacts`` table holds the entire identity graph.
  Instead of a generic graph (which would need recursive queries), we keep a
  flattened "star" topology: every cluster has exactly ONE primary row, and
  every other row in that cluster points at it via ``linkedId``.
  Consequence: resolving a full cluster is always a single indexed query
  (``id = P OR linkedId = P``) -- O(1) round trips, no recursion, no CTEs.

* Column names are kept in the camelCase form the spec asks for
  (``phoneNumber``, ``linkedId``, ``linkPrecedence`` ...) while the Python
  attributes stay snake_case. SQLAlchemy's ``Column("dbName", ...)`` gives us
  both without a translation layer.

* Soft deletes: ``deletedAt`` is never populated by this service, but every
  read path filters on it so that a future retention/GDPR job can tombstone
  rows without corrupting existing clusters.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./contacts.db")

engine = create_engine(
    DATABASE_URL,
    # SQLite only: FastAPI serves requests from a thread pool, and SQLite
    # connections are otherwise pinned to their creating thread.
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    # We read ORM attributes AFTER commit when building the response payload;
    # without this, SQLAlchemy would expire them and re-issue SELECTs.
    expire_on_commit=False,
    future=True,
)

Base = declarative_base()


def utcnow() -> datetime:
    """Naive UTC timestamp.

    SQLite has no native timezone support, so tz-aware values come back naive
    on the next read. Storing naive UTC everywhere keeps ``created_at``
    comparisons (which decide who stays primary) from blowing up with
    "can't compare offset-naive and offset-aware datetimes".
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class LinkPrecedence(str, enum.Enum):
    """Role of a row inside its cluster."""

    PRIMARY = "primary"
    SECONDARY = "secondary"


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True, autoincrement=True)

    phone_number = Column("phoneNumber", String(32), nullable=True)
    email = Column("email", String(320), nullable=True)

    # NULL for primaries; id of the cluster's primary for secondaries.
    linked_id = Column("linkedId", Integer, ForeignKey("contacts.id"), nullable=True)

    link_precedence = Column(
        "linkPrecedence",
        String(16),
        nullable=False,
        default=LinkPrecedence.PRIMARY.value,
    )

    created_at = Column("createdAt", DateTime, nullable=False, default=utcnow)
    updated_at = Column(
        "updatedAt", DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at = Column("deletedAt", DateTime, nullable=True)

    __table_args__ = (
        # /identify always looks rows up by email or phone -- without these two
        # indexes every request is a full table scan.
        Index("ix_contacts_email", "email"),
        Index("ix_contacts_phone_number", "phoneNumber"),
        # Cluster expansion: WHERE linkedId = :primary_id
        Index("ix_contacts_linked_id", "linkedId"),
        # Tie-breaking / reporting: oldest primary first.
        Index("ix_contacts_precedence_created", "linkPrecedence", "createdAt"),
    )

    # -- convenience ------------------------------------------------------- #

    @property
    def is_primary(self) -> bool:
        return self.link_precedence == LinkPrecedence.PRIMARY.value

    @property
    def cluster_root_id(self) -> int:
        """Id of the primary this row belongs to (itself, if it is the primary)."""
        return self.id if self.is_primary else self.linked_id

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"<Contact id={self.id} email={self.email!r} "
            f"phone={self.phone_number!r} precedence={self.link_precedence} "
            f"linkedId={self.linked_id}>"
        )


def init_db() -> None:
    """Create tables if they do not exist.

    Fine for this exercise; a real deployment would run Alembic migrations
    instead so that schema changes are versioned and reversible.
    """
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency: one session per request, always closed."""
    db: Optional[SessionLocal] = None
    try:
        db = SessionLocal()
        yield db
    finally:
        if db is not None:
            db.close()