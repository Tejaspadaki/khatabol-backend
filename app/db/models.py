import enum
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


_last_now: datetime | None = None


def utcnow() -> datetime:
    """Strictly increasing within the process. The Windows clock can return the same value for
    back-to-back calls, which would make "undo last entry" ambiguous."""
    global _last_now
    now = datetime.now(timezone.utc)
    if _last_now is not None and now <= _last_now:
        now = _last_now + timedelta(microseconds=1)
    _last_now = now
    return now


def as_utc(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat them as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def new_ref_code() -> str:
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(5))


def _enum(cls: type[enum.Enum]) -> Enum:
    return Enum(cls, native_enum=False, length=20)


class EntryType(str, enum.Enum):
    CREDIT = "CREDIT"
    PAYMENT = "PAYMENT"
    REVERSAL = "REVERSAL"


class EntrySource(str, enum.Enum):
    VOICE = "VOICE"
    UPI_AUTO = "UPI_AUTO"
    MANUAL = "MANUAL"


class PaymentMode(str, enum.Enum):
    CASH = "CASH"
    UPI = "UPI"
    OTHER = "OTHER"


class UpiTxnStatus(str, enum.Enum):
    APPLIED = "APPLIED"
    SALE = "SALE"
    UNMATCHED = "UNMATCHED"
    RESOLVED = "RESOLVED"


class PendingStatus(str, enum.Enum):
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(20))
    upi_vpa: Mapped[str | None] = mapped_column(String(100))
    language: Mapped[str] = mapped_column(String(10), default="hi")
    large_amount_threshold_paise: Mapped[int] = mapped_column(BigInteger, default=1_000_000)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        Index("ix_customers_merchant_phonetic", "merchant_id", "phonetic_key"),
        UniqueConstraint("merchant_id", "ref_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    name_normalized: Mapped[str] = mapped_column(String(200))
    phonetic_key: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(20))
    nicknames: Mapped[list[str]] = mapped_column(JSON, default=list)
    ref_code: Mapped[str] = mapped_column(String(10), default=new_ref_code)
    # Cached; the ledger is the source of truth. Positive = customer owes.
    balance_paise: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    upi_handles: Mapped[list["CustomerUpiHandle"]] = relationship(back_populates="customer")


class CustomerUpiHandle(Base):
    __tablename__ = "customer_upi_handles"
    __table_args__ = (UniqueConstraint("merchant_id", "vpa"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    vpa: Mapped[str] = mapped_column(String(100))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    linked_via: Mapped[str] = mapped_column(String(30))  # voice | payment_link | merchant_answer | manual
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    customer: Mapped[Customer] = relationship(back_populates="upi_handles")


class VoiceLog(Base):
    __tablename__ = "voice_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    session_id: Mapped[str] = mapped_column(String(64))
    audio_url: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str | None] = mapped_column(Text)
    stt_confidence: Mapped[float | None] = mapped_column(Float)
    llm_tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    reply: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(30))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LedgerEntry(Base):
    """Append-only. Corrections are REVERSAL entries, never updates."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="ck_ledger_amount_positive"),
        Index("ix_ledger_customer_created", "customer_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    type: Mapped[EntryType] = mapped_column(_enum(EntryType))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    due_date: Mapped[date | None] = mapped_column(Date)
    items: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[EntrySource] = mapped_column(_enum(EntrySource))
    payment_mode: Mapped[PaymentMode | None] = mapped_column(_enum(PaymentMode))
    upi_txn_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    reverses_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledger_entries.id"))
    voice_log_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("voice_logs.id"))
    spoken_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UpiTransaction(Base):
    """Every incoming UPI payment, matched or not. txn_id makes it idempotent."""

    __tablename__ = "upi_transactions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    txn_id: Mapped[str] = mapped_column(String(100), unique=True)
    payer_vpa: Mapped[str] = mapped_column(String(100))
    payer_name: Mapped[str | None] = mapped_column(String(200))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[UpiTxnStatus] = mapped_column(_enum(UpiTxnStatus))
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledger_entries.id"))
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PendingAction(Base):
    """A proposed write waiting for the merchant's "haan"."""

    __tablename__ = "pending_actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    session_id: Mapped[str] = mapped_column(String(64))
    tool: Mapped[str] = mapped_column(String(50))
    args: Mapped[dict] = mapped_column(JSON)
    summary: Mapped[str] = mapped_column(Text)
    confirmations_needed: Mapped[int] = mapped_column(Integer, default=1)
    confirmations_received: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[PendingStatus] = mapped_column(_enum(PendingStatus), default=PendingStatus.PENDING)
    voice_log_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("voice_logs.id"))
    result: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    channel: Mapped[str] = mapped_column(String(20))  # whatsapp | sms
    message: Mapped[str] = mapped_column(Text)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    payment_link: Mapped[str] = mapped_column(Text)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="SENT")  # mocked delivery
