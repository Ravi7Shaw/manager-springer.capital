from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Court, Payment, PaymentCancellationRecord, Reservation
from app.services.payments import PaymentCancellation, PaymentService


class BookingError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class BookingService:
    def __init__(self, payment_service: PaymentService | None = None,
                 payment_cancellation: PaymentCancellation | None = None) -> None:
        self.payment_service = payment_service or PaymentService()
        self.payment_cancellation = payment_cancellation or PaymentCancellation()

    @staticmethod
    def expire_holds(session: Session, now: datetime) -> None:
        session.execute(
            update(Reservation)
            .where(Reservation.status == "held", Reservation.hold_expires_at <= now)
            .values(status="expired")
        )

    @staticmethod
    def price_for(starts_at: datetime, ends_at: datetime) -> Decimal:
        # Baseline pricing policy: peak is 17:00–21:00 local/UTC; make venue-local pricing configurable later.
        peak = starts_at.hour >= 17 or ends_at.hour > 17
        return Decimal("800.00") if peak else Decimal("500.00")

    def create_hold(self, session: Session, *, court_id: UUID, member_id: UUID,
                    starts_at: datetime, ends_at: datetime, idempotency_key: str) -> Reservation:
        now = datetime.now(timezone.utc)
        if starts_at.tzinfo is None or ends_at.tzinfo is None:
            raise BookingError("starts_at and ends_at must include a timezone")
        if starts_at <= now or ends_at <= starts_at:
            raise BookingError("Booking interval must be positive and start in the future")
        with session.begin():
            existing = session.scalar(
                select(Reservation).where(Reservation.idempotency_key == idempotency_key)
            )
            if existing:
                if existing.member_id != member_id:
                    raise BookingError("Idempotency key belongs to another member", 409)
                return existing
            self.expire_holds(session, now)
            court = session.get(Court, court_id)
            if court is None or not court.active:
                raise BookingError("Court not found or inactive", 404)
            active_count = session.scalar(
                select(func.count()).select_from(Reservation).where(
                    Reservation.member_id == member_id,
                    Reservation.status.in_(["held", "confirmed"]),
                    Reservation.starts_at > now,
                )
            ) or 0
            if active_count >= 3:
                raise BookingError("Members may hold at most 3 active future bookings", 409)
            hold = Reservation(
                court_id=court_id, member_id=member_id, starts_at=starts_at, ends_at=ends_at,
                status="held", hold_expires_at=now + timedelta(seconds=settings.hold_ttl_seconds),
                price=self.price_for(starts_at, ends_at), currency="INR", idempotency_key=idempotency_key,
            )
            session.add(hold)
            try:
                session.flush()
            except IntegrityError as exc:
                raise BookingError("Court slot is unavailable", 409) from exc
        return hold

    def confirm(self, session: Session, reservation_id: UUID, member_id: UUID,
                idempotency_key: str) -> tuple[Reservation, Payment]:
        now = datetime.now(timezone.utc)
        with session.begin():
            self.expire_holds(session, now)
            reservation = session.scalar(
                select(Reservation).where(Reservation.id == reservation_id).with_for_update()
            )
            if reservation is None or reservation.member_id != member_id:
                raise BookingError("Hold not found", 404)
            payment_key = f"confirm:{idempotency_key}"
            payment = session.scalar(select(Payment).where(Payment.idempotency_key == payment_key))
            if reservation.status == "confirmed" and payment is not None:
                return reservation, payment
            if reservation.status != "held" or reservation.hold_expires_at is None or reservation.hold_expires_at <= now:
                raise BookingError("Hold has expired or is no longer confirmable", 409)
            reservation.status = "confirmed"
            reservation.hold_expires_at = None
            payment = self.payment_service.create_for_reservation(session, reservation, payment_key)
        return reservation, payment

    def cancel(self, session: Session, reservation_id: UUID, member_id: UUID,
               idempotency_key: str) -> tuple[Reservation, Decimal]:
        now = datetime.now(timezone.utc)
        with session.begin():
            reservation = session.scalar(
                select(Reservation).where(Reservation.id == reservation_id).with_for_update()
            )
            if reservation is None or reservation.member_id != member_id:
                raise BookingError("Booking not found", 404)
            if reservation.status == "cancelled":
                prior = session.scalar(select(PaymentCancellationRecord).where(
                    PaymentCancellationRecord.idempotency_key == f"cancel:{idempotency_key}"
                ))
                return reservation, prior.refund_amount if prior else Decimal("0.00")
            if reservation.status != "confirmed":
                raise BookingError("Only confirmed bookings can be cancelled", 409)
            payment = session.scalar(select(Payment).where(Payment.reservation_id == reservation.id))
            if payment is None:
                raise BookingError("Payment record missing", 500)
            refund = payment.amount if reservation.starts_at - now >= timedelta(hours=24) else Decimal("0.00")
            self.payment_cancellation.cancel(
                session, payment, refund, f"cancel:{idempotency_key}",
                "Member cancellation; full refund with at least 24 hours notice" if refund else
                "Member cancellation with less than 24 hours notice",
            )
            reservation.status = "cancelled"
        return reservation, refund
