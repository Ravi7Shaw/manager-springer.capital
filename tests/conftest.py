from collections.abc import Generator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import Base, SessionLocal, engine
from app.models import Court, Venue


@pytest.fixture
def db_session() -> Generator[tuple[Session, Court], None, None]:
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    venue = Venue(name=f"Test venue {uuid4()}", timezone="Asia/Kolkata")
    session.add(venue)
    session.flush()
    court = Court(venue_id=venue.id, name="Court 1", active=True)
    session.add(court)
    session.commit()
    try:
        yield session, court
    finally:
        session.close()
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM payment_cancellations"))
            connection.execute(text("DELETE FROM payments"))
            connection.execute(text("DELETE FROM reservations"))
            connection.execute(text("DELETE FROM courts"))
            connection.execute(text("DELETE FROM venues"))
