"""Payment boundary. Replace the mock provider with Stripe/Razorpay without changing routes."""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Payment, PaymentCancellationRecord, Reservation


@dataclass(frozen=True)
class RefundDecision:
    amount: Decimal
    reason: str


class PaymentProvider:
    """Provider adapter interface; this implementation is deliberately local/mock."""

    def charge(self, amount: Decimal, currency: str, idempotency_key: str) -> str:
        """Return a stable provider reference. Real adapters call the payment provider here."""
        return f"mock-charge:{idempotency_key}"

    def refund(self, provider_reference: str, amount: Decimal, idempotency_key: str) -> str:
        """Issue an idempotent refund in the configured provider."""
        return f"mock-refund:{idempotency_key}:{provider_reference}:{amount}"


class PaymentService:
    def __init__(self, provider: PaymentProvider | None = None) -> None:
        self.provider = provider or PaymentProvider()

    def create_for_reservation(
        self, session: Session, reservation: Reservation, idempotency_key: str
    ) -> Payment:
        existing = session.scalar(select(Payment).where(Payment.idempotency_key == idempotency_key))
        if existing:
            return existing
        reference = self.provider.charge(reservation.price, reservation.currency, idempotency_key)
        payment = Payment(
            reservation_id=reservation.id,
            member_id=reservation.member_id,
            amount=reservation.price,
            currency=reservation.currency,
            status="succeeded",
            provider_reference=reference,
            idempotency_key=idempotency_key,
        )
        session.add(payment)
        session.flush()
        return payment


class PaymentCancellation:
    """Owns refund policy and idempotent cancellation/refund records."""

    def __init__(self, provider: PaymentProvider | None = None) -> None:
        self.provider = provider or PaymentProvider()

    def cancel(
        self,
        session: Session,
        payment: Payment,
        refund_amount: Decimal,
        idempotency_key: str,
        reason: str,
    ) -> PaymentCancellationRecord:
        existing = session.scalar(
            select(PaymentCancellationRecord).where(
                PaymentCancellationRecord.idempotency_key == idempotency_key
            )
        )
        if existing:
            return existing
        if payment.status != "succeeded":
            refund_amount = Decimal("0.00")
        if refund_amount > 0 and payment.provider_reference:
            self.provider.refund(payment.provider_reference, refund_amount, idempotency_key)
        cancellation = PaymentCancellationRecord(
            payment_id=payment.id,
            idempotency_key=idempotency_key,
            refund_amount=refund_amount,
            status="succeeded",
            reason=reason,
        )
        if refund_amount == payment.amount:
            payment.status = "refunded"
        elif refund_amount > 0:
            payment.status = "partially_refunded"
        session.add(cancellation)
        session.flush()
        return cancellation
