from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.db.databases import Base
from app.core.db.types import UUIDChar

if TYPE_CHECKING:
    from app.models.knowledge import KnowledgeChunk
    from app.models.prescriptions import Prescription
    from app.models.profiles import Profile


class ChatSessionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class ChatRole(StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


class ChatGenerationStatus(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"
    GENERATING = "GENERATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ChatCitationClaimKind(StrEnum):
    CRITICAL = "CRITICAL"
    NON_CRITICAL = "NON_CRITICAL"


class ChatCitationVerdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"


class ChatSession(Base):
    __tablename__ = "chat_session"
    __table_args__ = (
        ForeignKeyConstraint(
            ["prescription_id", "profile_id"],
            ["prescription.id", "prescription.profile_id"],
            name="fk_chat_session_prescription_profile",
        ),
        ForeignKeyConstraint(
            ["prescription_version_id", "prescription_id"],
            ["prescription_version.id", "prescription_version.prescription_id"],
            name="fk_chat_session_prescription_version_prescription",
        ),
        Index(
            "idx_chat_session_prescription_activity",
            "prescription_id",
            "session_status",
            "last_message_at",
            "id",
        ),
        Index("idx_chat_session_profile_activity", "profile_id", "last_message_at", "id"),
        CheckConstraint("session_status IN ('ACTIVE', 'CLOSED')", name="chk_chat_session_status"),
    )

    id: Mapped[UUID] = mapped_column(UUIDChar(), primary_key=True, default=uuid4)
    prescription_id: Mapped[UUID] = mapped_column(UUIDChar(), nullable=False)
    prescription_version_id: Mapped[UUID] = mapped_column(UUIDChar(), nullable=False)
    profile_id: Mapped[UUID] = mapped_column(UUIDChar(), ForeignKey("profile.id"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_status: Mapped[ChatSessionStatus] = mapped_column(
        Enum(ChatSessionStatus, native_enum=False, length=20),
        nullable=False,
        default=ChatSessionStatus.ACTIVE,
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    prescription: Mapped["Prescription"] = relationship(back_populates="chat_sessions")
    profile: Mapped["Profile"] = relationship(overlaps="prescription")
    messages: Mapped[list["ChatMessage"]] = relationship(back_populates="session")


class ChatMessage(Base):
    __tablename__ = "chat_message"
    __table_args__ = (
        UniqueConstraint("session_id", "message_seq", name="uq_chat_message_session_seq"),
        CheckConstraint("role IN ('USER', 'ASSISTANT')", name="chk_chat_message_role"),
        CheckConstraint(
            "generation_status IN ('NOT_APPLICABLE', 'PENDING', 'GENERATING', 'COMPLETED', 'FAILED')",
            name="chk_chat_message_generation_status",
        ),
        CheckConstraint("message_seq > 0", name="chk_chat_message_seq"),
    )

    id: Mapped[UUID] = mapped_column(UUIDChar(), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(UUIDChar(), ForeignKey("chat_session.id"), nullable=False)
    message_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[ChatRole] = mapped_column(Enum(ChatRole, native_enum=False, length=20), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation_status: Mapped[ChatGenerationStatus] = mapped_column(
        Enum(ChatGenerationStatus, native_enum=False, length=20),
        nullable=False,
        default=ChatGenerationStatus.NOT_APPLICABLE,
    )
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    session: Mapped["ChatSession"] = relationship(back_populates="messages")
    citations: Mapped[list["ChatCitation"]] = relationship(
        back_populates="message",
        order_by="ChatCitation.display_order",
    )


class ChatCitation(Base):
    __tablename__ = "chat_citation"
    __table_args__ = (
        UniqueConstraint("message_id", "display_order", name="uq_chat_citation_order"),
        UniqueConstraint("message_id", "claim_key", name="uq_chat_citation_claim_key"),
        CheckConstraint("display_order > 0", name="chk_chat_citation_order"),
        CheckConstraint("claim_kind IN ('CRITICAL', 'NON_CRITICAL')", name="chk_chat_citation_claim_kind"),
        CheckConstraint("verdict IN ('SUPPORTED', 'NEEDS_VERIFICATION')", name="chk_chat_citation_verdict"),
        CheckConstraint(
            "NOT (claim_kind = 'CRITICAL' AND verdict = 'NEEDS_VERIFICATION')",
            name="chk_chat_citation_fail_closed_verdict",
        ),
        CheckConstraint(
            "(verdict = 'SUPPORTED' AND knowledge_chunk_id IS NOT NULL AND content_sha256 IS NOT NULL) "
            "OR (verdict = 'NEEDS_VERIFICATION')",
            name="chk_chat_citation_supported_shape",
        ),
        CheckConstraint(
            "content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'",
            name="chk_chat_citation_content_hash",
        ),
        CheckConstraint(
            "length(trim(claim_key)) > 0 AND length(trim(claim_text)) > 0 "
            "AND length(trim(source_code)) > 0 AND length(trim(source_version)) > 0 "
            "AND length(trim(locator)) > 0",
            name="chk_chat_citation_nonblank_fields",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDChar(), primary_key=True, default=uuid4)
    message_id: Mapped[UUID] = mapped_column(UUIDChar(), ForeignKey("chat_message.id"), nullable=False)
    knowledge_chunk_id: Mapped[UUID | None] = mapped_column(UUIDChar(), ForeignKey("knowledge_chunk.id"), nullable=True)
    claim_key: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_text: Mapped[str] = mapped_column(String(1000), nullable=False)
    claim_kind: Mapped[ChatCitationClaimKind] = mapped_column(
        Enum(ChatCitationClaimKind, native_enum=False, length=20),
        nullable=False,
    )
    verdict: Mapped[ChatCitationVerdict] = mapped_column(
        Enum(ChatCitationVerdict, native_enum=False, length=30),
        nullable=False,
    )
    source_code: Mapped[str] = mapped_column(String(100), nullable=False)
    source_version: Mapped[str] = mapped_column(String(200), nullable=False)
    locator: Mapped[str] = mapped_column(String(500), nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cited_text: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    message: Mapped["ChatMessage"] = relationship(back_populates="citations")
    knowledge_chunk: Mapped["KnowledgeChunk | None"] = relationship(back_populates="chat_citations")
