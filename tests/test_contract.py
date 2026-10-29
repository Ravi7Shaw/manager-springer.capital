from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services.bookings import BookingService


def test_peak_pricing_is_higher_than_off_peak() -> None:
    start = datetime(2030, 1, 2, 10, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    peak_start = datetime(2030, 1, 2, 18, tzinfo=timezone.utc)
    peak_end = peak_start + timedelta(hours=1)
    assert BookingService.price_for(start, end) == Decimal("500.00")
    assert BookingService.price_for(peak_start, peak_end) == Decimal("800.00")
