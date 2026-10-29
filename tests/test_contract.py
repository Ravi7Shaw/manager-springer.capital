from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.bookings import BookingService


def test_peak_pricing_is_higher_than_off_peak() -> None:
    start = datetime(2030, 1, 2, 10, tzinfo=UTC)
    end = start + timedelta(hours=1)
    peak_start = datetime(2030, 1, 2, 18, tzinfo=UTC)
    peak_end = peak_start + timedelta(hours=1)
    assert BookingService.price_for(start, end) == Decimal("500.00")
    assert BookingService.price_for(peak_start, peak_end) == Decimal("800.00")
