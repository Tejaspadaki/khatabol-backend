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
    event,
    false as sa_false,
    true as sa_true,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

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
    ADJUSTMENT = "ADJUSTMENT"  # waiver / discount: lowers what is owed, but is not money received (needs a reason)
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
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # a staff edit, confirmed by staff, waiting for the owner (§19.3)


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(20))
    upi_vpa: Mapped[str | None] = mapped_column(String(100))
    language: Mapped[str] = mapped_column(String(10), default="hi")
    large_amount_threshold_paise: Mapped[int] = mapped_column(BigInteger, default=1_000_000)
    # The daily job holds a customer whose udhaar is overdue by more than this many days.
    hold_grace_days: Mapped[int] = mapped_column(Integer, default=15, server_default="15")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Family(Base):
    """A household that shares one khata (plan.md §17). Its udhaar is posted on the head's khata with
    family_id set, and taken_by_customer_id says which member took the goods."""

    __tablename__ = "families"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(200))  # "Sharma parivar"
    name_normalized: Mapped[str] = mapped_column(String(200))
    head_customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", use_alter=True, name="fk_families_head"))
    area_hint: Mapped[str | None] = mapped_column(Text)  # "gali no. 3", to tell two Sharma families apart
    credit_limit_paise: Mapped[int | None] = mapped_column(BigInteger)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("families.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FamilyMerge(Base):
    """Audit of a family merge (duplicates created by mistake). Append-only."""

    __tablename__ = "family_merges"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    from_family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id"))
    into_family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id"))
    moved: Mapped[dict] = mapped_column(JSON)  # members, entries (old -> new), upi ids
    merged_by: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CustomerAccount(Base):
    """One person, keyed by phone, across every shop that uses Khata Bol (plan.md §18.3)."""

    __tablename__ = "customer_accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    phone: Mapped[str] = mapped_column(String(20), unique=True)  # E.164, +91XXXXXXXXXX
    name: Mapped[str | None] = mapped_column(String(200))
    preferred_language: Mapped[str | None] = mapped_column(String(10))
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
    # NULL = no limit; 0 = no new udhaar. Every change is a CREDIT_LIMIT_SET row in customer_events.
    credit_limit_paise: Mapped[int | None] = mapped_column(BigInteger)
    # One person across shops (plan.md §18.3): set from the phone number.
    customer_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customer_accounts.id"), index=True)
    notify_pref: Mapped[str] = mapped_column(String(10), default="instant", server_default="instant")  # instant|digest|off
    # Family khata (plan.md §17). Members need no phone; only the head's contact matters.
    family_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("families.id", name="fk_customers_family_id"), index=True)
    relation_to_head: Mapped[str | None] = mapped_column(String(20))  # self, beta, beti, bahu, patni, pati, bhai ...
    can_take_credit: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_true())
    member_limit_paise: Mapped[int | None] = mapped_column(BigInteger)
    # Cache of the latest HOLD_PLACED / HOLD_LIFTED event. A hold blocks new udhaar only.
    on_hold: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    hold_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    upi_handles: Mapped[list["CustomerUpiHandle"]] = relationship(back_populates="customer")


class CustomerEvent(Base):
    """Append-only audit of customer-level changes (limits, holds). Never updated or deleted."""

    __tablename__ = "customer_events"
    __table_args__ = (
        CheckConstraint("actor IN ('system', 'merchant', 'customer')", name="ck_customer_events_actor"),
        Index("ix_customer_events_customer_created", "customer_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


APPEND_ONLY: tuple[type, ...] = ()  # filled in at the bottom of this module


@event.listens_for(Session, "before_flush")
def _append_only_guard(session: Session, _ctx, _instances) -> None:
    """Append-only tables are never updated or deleted; corrections are new rows.

    Tests may back-date `created_at` to build a history (session.info["allow_backdate"]); nothing else.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.ledger.errors import LedgerError

    for o in session.deleted:
        if isinstance(o, APPEND_ONLY):
            raise LedgerError(f"{o.__tablename__} is append-only.")
    for o in session.dirty:
        if not isinstance(o, APPEND_ONLY) or not session.is_modified(o):
            continue
        changed = {a.key for a in sa_inspect(o).attrs if a.history.has_changes()}
        if not (session.info.get("allow_backdate") and changed <= {"created_at"}):
            raise LedgerError(f"{o.__tablename__} is append-only.")


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
    reason: Mapped[str | None] = mapped_column(Text)  # why: waiver reason, edit reason
    family_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("families.id", name="fk_ledger_family_id"))  # who owes (§17)
    taken_by_customer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customers.id", name="fk_ledger_taken_by"))  # member who took / paid
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
    # Partial-payment nudges: at most one per payment per day. NULLs (ordinary reminders) never collide.
    __table_args__ = (UniqueConstraint("source_payment_id", "nudge_date", name="uq_reminders_payment_nudge_day"),)

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
    source_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ledger_entries.id", name="fk_reminders_source_payment_id"))
    nudge_date: Mapped[date | None] = mapped_column(Date)  # IST day the nudge was sent


class PaymentAllocation(Base):
    """Which payment (or waiver) paid which udhaar (plan.md §20). Append-only: a reversal adds a
    negative row pointing at the reversal entry; the net of a (payment, credit) pair is its sum."""

    __tablename__ = "payment_allocations"
    __table_args__ = (
        CheckConstraint("amount_paise <> 0", name="ck_allocation_nonzero"),
        Index("ix_allocations_credit", "credit_entry_id"),
        Index("ix_allocations_payment", "payment_entry_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    payment_entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_entries.id"))
    credit_entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_entries.id"))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    reversal_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledger_entries.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Staff(Base):
    """People who use the shop's khata. role owner = may edit and approve; staff = edits need approval."""

    __tablename__ = "staff"
    __table_args__ = (CheckConstraint("role IN ('owner', 'staff')", name="ck_staff_role"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(10), default="staff")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EntryEdit(Base):
    """One changed field of one edit (plan.md §19). Append-only. An edit's ledger side is a reversal of the
    original plus a corrected new entry; notes and cancels have no new entry."""

    __tablename__ = "entry_edits"
    __table_args__ = (Index("ix_entry_edits_original", "original_entry_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    original_entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_entries.id"))
    reversal_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledger_entries.id"))
    new_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledger_entries.id"))
    field_changed: Mapped[str] = mapped_column(String(30))  # amount | due_date | items | customer | note | cancel
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    edited_by: Mapped[str] = mapped_column(String(60))  # 'owner' or 'staff:<id>'
    approved_by: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Notification(Base):
    """A message to a customer about a change to their khata (plan.md §18). Append-only: recorded before
    it is sent; what happened on delivery goes in notification_deliveries."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_customer_created", "customer_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    event_type: Mapped[str] = mapped_column(String(20))  # CREDIT PAYMENT PARTIAL_PAYMENT EDIT REVERSAL WAIVER POINTS DISPUTE DIGEST
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledger_entries.id"))
    channel: Mapped[str] = mapped_column(String(10), default="whatsapp")  # first choice; SMS is the fallback
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    status: Mapped[str] = mapped_column(String(12))  # QUEUED | DIGEST | NO_PHONE | OPTED_OUT
    message: Mapped[str] = mapped_column(Text)
    send_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # quiet hours / batch window
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationDelivery(Base):
    """Append-only delivery attempts: SENT / FAILED per channel, or DIGESTED into a daily summary."""

    __tablename__ = "notification_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    notification_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notifications.id"), index=True)
    channel: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10))  # SENT | FAILED | DIGESTED
    detail: Mapped[str | None] = mapped_column(Text)  # provider ref, error, or the digest notification id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Dispute(Base):
    """"Galat hai" on an entry (plan.md §18.4). While OPEN, reminders and calls skip that entry."""

    __tablename__ = "disputes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    ledger_entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_entries.id"))
    raised_by: Mapped[str] = mapped_column(String(10))  # customer | call | merchant
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="OPEN")  # OPEN | CONFIRMED | EDITED
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PassbookOtp(Base):
    __tablename__ = "passbook_otps"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_accounts.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PassbookSession(Base):
    __tablename__ = "passbook_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


APPEND_ONLY = (LedgerEntry, CustomerEvent, PaymentAllocation, EntryEdit, Notification, NotificationDelivery, FamilyMerge)
