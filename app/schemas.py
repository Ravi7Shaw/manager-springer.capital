from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class HoldCreate(BaseModel):
    court_id: UUID
    member_id: UUID
    starts_at: datetime
    ends_at: datetime
    idempotency_key: str = Field(min_length=8, max_length=200)


class HoldResponse(BaseModel):
    id: UUID
    court_id: UUID
    member_id: UUID
    starts_at: datetime
    ends_at: datetime
    status: str
    hold_expires_at: datetime | None
    price: Decimal
    currency: str
    model_config = ConfigDict(from_attributes=True)


class ConfirmRequest(BaseModel):
    member_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=200)


class CancelRequest(BaseModel):
    member_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=200)


class AvailabilityResponse(BaseModel):
    court_id: UUID
    starts_at: datetime
    ends_at: datetime
    available: bool


class PaymentResponse(BaseModel):
    id: UUID
    reservation_id: UUID
    amount: Decimal
    currency: str
    status: str
    model_config = ConfigDict(from_attributes=True)
