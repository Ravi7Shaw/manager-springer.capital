import argparse

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Court, Venue


def create_court(venue_name: str, court_name: str, timezone: str) -> dict[str, str | object]:
    with SessionLocal.begin() as session:
        venue = session.scalar(select(Venue).where(Venue.name == venue_name).limit(1))

        if venue is None:
            venue = Venue(name=venue_name, timezone=timezone)
            session.add(venue)
            session.flush()
            print(f"[+] Venue created: {venue.name}")
        else:
            print(f"[=] Venue already exists: {venue.name}")

        court = session.scalar(
            select(Court)
            .where(
                Court.venue_id == venue.id,
                Court.name == court_name,
            )
            .limit(1)
        )

        if court is None:
            court = Court(
                venue_id=venue.id,
                name=court_name,
                active=True,
            )
            session.add(court)
            session.flush()
            print(f"[+] Court created: {court.name}")
        else:
            print(f"[=] Court already exists: {court.name}")

        result = {
            "venue_id": str(venue.id),
            "venue_name": venue.name,
            "court_id": str(court.id),
            "court_name": court.name,
            "active": court.active,
        }

    return result


def view_courts() -> None:
    with SessionLocal() as session:
        statement = (
            select(Venue, Court)
            .join(Court, Court.venue_id == Venue.id)
            .order_by(Venue.name, Court.name)
        )

        results = session.execute(statement).all()

        if not results:
            print("\nNo venues or courts found.")
            return

        print("\n" + "=" * 100)
        print(f"{'VENUE':<25} {'COURT':<22} {'STATUS':<12} {'COURT UUID'}")
        print("=" * 100)

        for venue, court in results:
            status = "ACTIVE" if court.active else "INACTIVE"

            print(f"{venue.name:<25} {court.name:<22} {status:<12} {court.id}")

            print(f"  Venue UUID: {venue.id}")
            print(f"  Timezone:   {venue.timezone}")
            print("-" * 100)


def main() -> None:
    parser = argparse.ArgumentParser(description="CourtBook development management CLI")

    parser.add_argument("--venue", default="Central Venue")
    parser.add_argument("--court", default="Court 1")
    parser.add_argument("--timezone", default="Asia/Kolkata")

    parser.add_argument(
        "--list",
        "--view",
        action="store_true",
        dest="list_courts",
        help="View all venues and courts",
    )

    args = parser.parse_args()

    try:
        if args.list_courts:
            view_courts()
            return

        result = create_court(
            venue_name=args.venue,
            court_name=args.court,
            timezone=args.timezone,
        )

        print("\n========== RESULT ==========")

        for key, value in result.items():
            print(f"{key}: {value}")

        print("============================")

    except Exception as exc:
        print(f"\n[ERROR] {exc}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
