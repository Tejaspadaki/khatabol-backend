import uuid
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class MerchantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = None
    upi_vpa: str | None = None
    language: Literal["hi", "mr", "en"] = "hi"
    large_amount_threshold_rupees: float = Field(10_000, gt=0)


class MerchantUpdate(BaseModel):
    """Partial update: only the fields sent are changed."""

    name: str | None = Field(None, min_length=1, max_length=200)
    phone: str | None = None
    upi_vpa: str | None = None
    language: Literal["hi", "mr", "en"] | None = None
    large_amount_threshold_rupees: float | None = Field(None, gt=0)
    hold_grace_days: int | None = Field(None, ge=0, le=365, description="Overdue days before the daily hold")


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = None
    nicknames: list[str] = []


class CreditIn(BaseModel):
    amount_rupees: float = Field(gt=0)
    due: str | None = Field(None, description="YYYY-MM-DD or spoken: 'somvar', '5 tareekh'")
    items: str | None = None
    note: str | None = None


class PaymentIn(BaseModel):
    amount_rupees: float = Field(gt=0)
    mode: Literal["cash", "upi", "other"] = "cash"
    note: str | None = None


class UpiLinkIn(BaseModel):
    vpa: str


class AgentMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    session_id: str | None = Field(None, max_length=64)
    staff_id: uuid.UUID | None = Field(None, description="Who is speaking; omit for the owner")
    speak: bool = Field(False, description="Also return the reply as Sarvam TTS audio")


class AgentProposeIn(BaseModel):
    """A write proposed by a button in the app. Only tools meant for one-tap use are allowed."""

    tool: Literal["send_partial_nudge", "edit_entry", "waive_amount", "create_family", "add_member", "remove_member",
                  "merge_families"]
    args: dict = {}
    session_id: str | None = Field(None, max_length=64)
    staff_id: uuid.UUID | None = None
    speak: bool = False


class StaffIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role: Literal["owner", "staff"] = "staff"


class DisputeConfirmIn(BaseModel):
    note: str | None = Field(None, max_length=500)


class OtpIn(BaseModel):
    phone: str = Field(min_length=10, max_length=20)


class PassbookLoginIn(BaseModel):
    phone: str = Field(min_length=10, max_length=20)
    code: str = Field(min_length=4, max_length=8)


class PassbookDisputeIn(BaseModel):
    entry_id: uuid.UUID
    reason: str | None = Field(None, max_length=500)


class PassbookPrefIn(BaseModel):
    merchant_id: uuid.UUID
    mode: Literal["instant", "digest", "off"]


class InboundMessageIn(BaseModel):
    phone: str = Field(min_length=10, max_length=20)
    text: str = Field(max_length=1000)


class TtsIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ReminderIn(BaseModel):
    customer_ids: list[uuid.UUID] = []
    all_due: bool = False

    @model_validator(mode="after")
    def _one_target(self):
        if not self.customer_ids and not self.all_due:
            raise ValueError("give customer_ids or all_due=true")
        return self


class IncomingPaymentIn(BaseModel):
    """A UPI payment received by the merchant. Simulated in this prototype; in production this
    would come from the merchant's payment platform."""

    payer_vpa: str = Field(min_length=3, max_length=100)
    amount_rupees: float = Field(gt=0)
    payer_name: str | None = None
    note: str | None = Field(None, description="UPI note; reminder links put KHATA-<ref> here")
    txn_id: str | None = Field(None, max_length=100, description="Auto-generated if omitted")


class AssignIn(BaseModel):
    customer_id: uuid.UUID
