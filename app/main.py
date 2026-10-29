from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base, engine, get_db
from app.models import Court, Reservation, Venue
from app.schemas import (
    AvailabilityResponse,
    CancelRequest,
    ConfirmRequest,
    HoldCreate,
    HoldResponse,
    PaymentResponse,
)
from app.services.bookings import BookingError, BookingService


@asynccontextmanager
async def lifespan(_: FastAPI):
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="CourtBook API", version="1.0.0", lifespan=lifespan)
booking_service = BookingService()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/holds", response_model=HoldResponse, status_code=201)
def create_hold(payload: HoldCreate, session: Session = Depends(get_db)) -> HoldResponse:
    try:
        hold = booking_service.create_hold(
            session,
            court_id=payload.court_id,
            member_id=payload.member_id,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            idempotency_key=payload.idempotency_key,
        )
        return HoldResponse.model_validate(hold)
    except BookingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/holds/{reservation_id}/confirm", response_model=PaymentResponse)
def confirm_hold(
    reservation_id: UUID, payload: ConfirmRequest, session: Session = Depends(get_db)
) -> PaymentResponse:
    try:
        _, payment = booking_service.confirm(
            session, reservation_id, payload.member_id, payload.idempotency_key
        )
        return PaymentResponse.model_validate(payment)
    except BookingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/bookings/{reservation_id}/cancel")
def cancel_booking(
    reservation_id: UUID, payload: CancelRequest, session: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        reservation, refund = booking_service.cancel(
            session, reservation_id, payload.member_id, payload.idempotency_key
        )
        return {
            "booking_id": reservation.id,
            "status": reservation.status,
            "refund_amount": refund,
            "currency": reservation.currency,
        }
    except BookingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/courts/{court_id}/availability", response_model=AvailabilityResponse)
def availability(
    court_id: UUID,
    starts_at: datetime = Query(),
    ends_at: datetime = Query(),
    session: Session = Depends(get_db),
) -> AvailabilityResponse:
    if starts_at.tzinfo is None or ends_at.tzinfo is None or ends_at <= starts_at:
        raise HTTPException(status_code=422, detail="Provide a valid timezone-aware interval")
    now = datetime.now(UTC)
    with session.begin():
        booking_service.expire_holds(session, now)
        court = session.get(Court, court_id)
        if court is None or not court.active:
            raise HTTPException(status_code=404, detail="Court not found")
        conflict = session.scalar(
            select(Reservation.id)
            .where(
                Reservation.court_id == court_id,
                Reservation.status.in_(["held", "confirmed"]),
                Reservation.starts_at < ends_at,
                Reservation.ends_at > starts_at,
            )
            .limit(1)
        )
    return AvailabilityResponse(
        court_id=court_id, starts_at=starts_at, ends_at=ends_at, available=conflict is None
    )


@app.post("/recurring/weekly")
def create_weekly_bookings(
    court_id: UUID,
    member_id: UUID,
    starts_at: datetime,
    ends_at: datetime,
    weeks: int = Query(ge=1, le=52),
    idempotency_key: str = Header(min_length=8),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """Create a weekly series atomically: any conflict rolls back the entire series."""
    if starts_at.tzinfo is None or ends_at.tzinfo is None or ends_at <= starts_at:
        raise HTTPException(status_code=422, detail="Provide a valid timezone-aware interval")
    created: list[Reservation] = []
    try:
        with session.begin():
            now = datetime.now(UTC)
            booking_service.expire_holds(session, now)
            existing_count = (
                session.scalar(
                    select(func.count())
                    .select_from(Reservation)
                    .where(
                        Reservation.member_id == member_id,
                        Reservation.status.in_(["held", "confirmed"]),
                        Reservation.starts_at > now,
                    )
                )
                or 0
            )
            if existing_count + weeks > 3:
                raise HTTPException(
                    status_code=409, detail="Weekly series would exceed the 3-booking limit"
                )
            if session.get(Court, court_id) is None:
                raise HTTPException(status_code=404, detail="Court not found")
            for week in range(weeks):
                start = starts_at + timedelta(days=7 * week)
                end = ends_at + timedelta(days=7 * week)
                hold = Reservation(
                    court_id=court_id,
                    member_id=member_id,
                    starts_at=start,
                    ends_at=end,
                    status="held",
                    hold_expires_at=None,
                    price=booking_service.price_for(start, end),
                    currency="INR",
                    idempotency_key=f"{idempotency_key}:{week}",
                )
                # Recurring series is confirmed only after every slot has been reserved.
                hold.status = "confirmed"
                session.add(hold)
                session.flush()
                created.append(hold)
    except (IntegrityError, BookingError) as exc:
        raise HTTPException(
            status_code=409, detail="Weekly series conflicts; no bookings created"
        ) from exc
    return {"created": len(created), "booking_ids": [item.id for item in created]}


@app.get("/staff/utilization/weekly")
def weekly_utilization(
    week_start: datetime, venue_id: UUID | None = None, session: Session = Depends(get_db)
) -> list[dict[str, object]]:
    if week_start.tzinfo is None:
        raise HTTPException(status_code=422, detail="week_start must include timezone")
    week_end = week_start + timedelta(days=7)
    statement = (
        select(
            Court.id.label("court_id"),
            Court.name.label("court_name"),
            func.coalesce(
                func.sum(
                    func.extract(
                        "epoch",
                        func.least(Reservation.ends_at, week_end)
                        - func.greatest(Reservation.starts_at, week_start),
                    )
                    / 3600
                ),
                0,
            ).label("booked_hours"),
        )
        .join(Venue, Venue.id == Court.venue_id)
        .outerjoin(
            Reservation,
            (Reservation.court_id == Court.id)
            & (Reservation.status == "confirmed")
            & (Reservation.starts_at < week_end)
            & (Reservation.ends_at > week_start),
        )
        .group_by(Court.id, Court.name)
    )
    if venue_id:
        statement = statement.where(Venue.id == venue_id)
    return [
        {
            "court_id": row.court_id,
            "court_name": row.court_name,
            "booked_hours": float(row.booked_hours or 0),
        }
        for row in session.execute(statement)
    ]
