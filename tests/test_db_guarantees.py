from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import Payment, Reservation
from app.services.bookings import BookingError, BookingService


def future_interval() -> tuple[datetime, datetime]:
    start = datetime.now(timezone.utc) + timedelta(days=5)
    start = start.replace(minute=0, second=0, microsecond=0)
    return start, start + timedelta(hours=1)


def test_database_rejects_overlapping_active_reservations(db_session) -> None:
    session, court = db_session
    service = BookingService()
    start, end = future_interval()
    service.create_hold(session, court_id=court.id, member_id=uuid4(), starts_at=start,
                        ends_at=end, idempotency_key=f"hold-{uuid4()}")
    with pytest.raises(BookingError, match="unavailable"):
        service.create_hold(session, court_id=court.id, member_id=uuid4(), starts_at=start,
                            ends_at=end, idempotency_key=f"hold-{uuid4()}")


def test_confirm_retry_with_same_key_creates_one_payment(db_session) -> None:
    session, court = db_session
    service = BookingService()
    start, end = future_interval()
    member = uuid4()
    hold = service.create_hold(session, court_id=court.id, member_id=member, starts_at=start,
                               ends_at=end, idempotency_key=f"hold-{uuid4()}")
    key = f"confirm-{uuid4()}"
    _, first = service.confirm(session, hold.id, member, key)
    _, retry = service.confirm(session, hold.id, member, key)
    assert first.id == retry.id
    assert session.scalar(select(Payment).where(Payment.reservation_id == hold.id)) is not None
    assert len(session.scalars(select(Payment).where(Payment.reservation_id == hold.id)).all()) == 1


def test_failed_multi_step_operation_rolls_back_database_state(db_session) -> None:
    session, court = db_session
    service = BookingService()
    start, end = future_interval()
    service.create_hold(session, court_id=court.id, member_id=uuid4(), starts_at=start,
                        ends_at=end, idempotency_key=f"hold-{uuid4()}")
    before = len(session.scalars(select(Reservation)).all())
    with pytest.raises(BookingError):
        service.create_hold(session, court_id=uuid4(), member_id=uuid4(), starts_at=start,
                            ends_at=end, idempotency_key=f"hold-{uuid4()}")
    session.expire_all()
    after = len(session.scalars(select(Reservation)).all())
    assert after == before
