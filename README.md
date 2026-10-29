# CourtBook API

A Python 3.11 / FastAPI court booking starter using SQLAlchemy 2, PostgreSQL 15, pytest, Ruff, and mypy. PostgreSQL is the source of truth; Redis is optional and deliberately not required for booking correctness. On Linux, Compose uses host networking for the API, database, and optional Redis; the image build also uses the host network for dependency installation.

## Run with Docker

```bash
docker compose up --build
```

Open API docs at <http://localhost:8000/docs> and health at <http://localhost:8000/health>.

Host networking means ports 5432 (PostgreSQL), 8000 (API), and optionally 6379 (Redis) are bound directly on the host; Compose `ports:` mappings are not used. Ensure these ports are free and do not expose PostgreSQL to untrusted networks. The host-network setup is intended for Linux Docker Engine.

Redis is off by default. To start the optional Redis service:

```bash
docker compose --profile cache up --build
```

Removing the Redis service does not affect booking operations. The current starter does not yet use Redis in its request path. Because Redis binds to `127.0.0.1`, it is not reachable from other machines by default.

## Required API contract

- `POST /holds` — reserve a court interval for five minutes.
- `POST /holds/{id}/confirm` — confirm the hold using an idempotency key.
- `POST /bookings/{id}/cancel` — cancel and apply the 24-hour refund rule.
- `GET /courts/{id}/availability?starts_at=...&ends_at=...` — check current availability.

Additional endpoints: `POST /recurring/weekly`, `GET /staff/utilization/weekly`, and `GET /health`.

### First-run setup

The database starts empty. Create a venue and court in a local SQL session before booking:

```sql
INSERT INTO venues (id, name, timezone)
VALUES ('11111111-1111-4111-8111-111111111111', 'Central Venue', 'Asia/Kolkata');
INSERT INTO courts (id, venue_id, name, active)
VALUES ('22222222-2222-4222-8222-222222222222',
        '11111111-1111-4111-8111-111111111111', 'Court 1', true);
```

Example hold body:

```json
{
  "court_id": "22222222-2222-4222-8222-222222222222",
  "member_id": "33333333-3333-4333-8333-333333333333",
  "starts_at": "2030-01-01T10:00:00+05:30",
  "ends_at": "2030-01-01T11:00:00+05:30",
  "idempotency_key": "hold-demo-0001"
}
```

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
ruff check .
mypy app
pytest -q
```

Set `DATABASE_URL` to a local PostgreSQL 15 instance and create the `btree_gist` extension; the application startup creates it when permitted.

## Design and safety notes

Read [DESIGN.md](DESIGN.md) for assumptions and guarantee reasoning and [LIMITATIONS.md](LIMITATIONS.md) before deployment. Payment methods are intentionally adapter-based and currently mock-only. This is a learning/engineering starter, not a production-ready payment system.
