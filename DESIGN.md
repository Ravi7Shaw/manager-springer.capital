# CourtBook design

This document describes CourtBook as it exists in the repository at commit `c079601` ("Fixed configuration"). It replaces the earlier DESIGN.md, which described several behaviors the code does not implement. Where code and earlier documentation disagree, this document follows the code and records the gap in section 12.

Verification notes: items marked **reproduced** were run through the HTTP API against a local PostgreSQL 16 database with the pinned requirements.txt. Items marked **by reading** follow from the source and were not executed. On that same setup, `pytest -q` reports 4 passed, `ruff check`, `ruff format --check`, and `mypy app` all pass. That run used Python 3.12. CI uses PostgreSQL 15 and Python 3.14, so CI results may differ slightly.

## 1. Purpose and scope

CourtBook is a FastAPI service that lets members book time on courts at a venue. It supports placing a five-minute hold on a court interval, confirming the hold with a payment, cancelling with a refund rule, booking a weekly recurring series, checking availability, and producing a weekly utilization report for staff.

It is a learning and engineering starter, not a production system. It has no authentication, the payment provider is a mock, and several correctness gaps are listed in section 12. Operational gaps (observability, backups, migrations, secrets) are tracked in LIMITATIONS.md.

## 2. Architecture

The code has four layers. The HTTP layer in `app/main.py` defines the routes and maps service errors to HTTP status codes. Request and response models live in `app/schemas.py`. The service layer in `app/services/bookings.py` holds the booking rules and transaction boundaries. The payment boundary in `app/services/payments.py` wraps a provider adapter; the only adapter, `PaymentProvider`, returns synthetic references and calls nothing external. The persistence layer is SQLAlchemy 2 models in `app/models/entities.py`, with one engine and session factory in `app/db.py`. PostgreSQL 15 is the system of record.

Redis appears in the settings, the Compose file, and requirements.txt, but no code path imports or calls it. Nothing in the request path depends on it.

Most routes delegate to `BookingService`. The exception is `POST /recurring/weekly`, whose handler performs the member-limit check, court lookup, pricing, and row inserts itself. That split is the source of several defects in section 12.

The Dockerfile starts a single uvicorn process with default settings. Each request receives its own database session from `get_db`, and the session closes when the request ends.

## 3. Data model

| Table | Key columns | Constraints and notes |
|---|---|---|
| `venues` | `id` (uuid), `name` (≤120), `timezone` (≤64, default `UTC`) | Timezone is stored but no calculation reads it. |
| `courts` | `id`, `venue_id` (FK, RESTRICT), `name` (≤120), `active` | Hold and availability endpoints reject inactive courts. The recurring endpoint does not check `active`. |
| `reservations` | `id`, `court_id` (FK, RESTRICT), `member_id` (indexed), `starts_at`, `ends_at` (timestamptz), `status`, `hold_expires_at`, `price` NUMERIC(10,2), `currency` (3 characters, default `INR`), `idempotency_key` (unique, nullable), `created_at` | CHECK `ends_at > starts_at`. CHECK status in `held`, `confirmed`, `cancelled`, `expired`. EXCLUDE USING gist `(court_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&) WHERE status IN ('held','confirmed')`. |
| `payments` | `id`, `reservation_id` (FK, unique), `member_id`, `amount`, `currency`, `status`, `provider_reference` (unique, nullable), `idempotency_key` (unique, not null) | One payment per reservation by intent. Status is written as `succeeded`; the model default `pending` is never used. |
| `payment_cancellations` | `id`, `payment_id` (FK), `idempotency_key` (unique), `refund_amount`, `status`, `reason`, `created_at` | One record per processed cancellation, including zero-refund cancellations. |

The exclusion constraint is the core of the booking model. It is the only mechanism that prevents two active reservations from overlapping on the same court. Because it applies only to `held` and `confirmed` rows, moving a row to `cancelled` or `expired` releases the slot immediately. Rows are never deleted, which preserves history.

### Reservation lifecycle

A hold is created as `held` with `hold_expires_at` set to creation time plus `HOLD_TTL_SECONDS`. Confirmation moves a `held` row to `confirmed` and clears the expiry. A `held` row whose expiry has passed becomes `expired` the next time any booking operation sweeps, which is request-driven and has no background worker in this repository. A `confirmed` booking becomes `cancelled` through the cancel endpoint. `cancelled` and `expired` are terminal.

Recurring series rows skip this lifecycle. They are inserted directly as `confirmed` with no `hold_expires_at` and no payment record (see defect D3).

## 4. Booking rules

### Time and intervals

All booking intervals are timezone-aware instants stored as `timestamptz`. Intervals are half-open, `[starts_at, ends_at)`, so 10:00–11:00 and 11:00–12:00 do not overlap. Naive datetimes are rejected: with a 400 from the service for holds, and with a 422 from request validation for availability and the recurring and utilization endpoints.

### Holds

A hold lasts `HOLD_TTL_SECONDS`, which defaults to 300 seconds. The start time must be in the future and the end must be after the start, both checked against the application clock. Only `POST /holds` enforces the future-start rule; the recurring endpoint does not (defect D4).

Each hold stores its price at creation time. Confirming later does not reprice it.

### Member limit

A member may have at most three reservations whose status is `held` or `confirmed` and whose start is after now. Unconfirmed holds count toward this limit. The earlier design document said they did not; the code does count them (reproduced: a fourth hold was rejected with 409 while three unconfirmed holds existed). The product decision is recorded in section 13, and the code is the current behavior.

The limit is checked by counting rows and then inserting, without a row lock. Two concurrent requests from the same member can both pass the check (see section 5).

### Pricing

`BookingService.price_for` charges INR 800 if the start hour is 17 or later, or if the end hour is later than 17. Otherwise it charges INR 500. The hour is read from the datetime exactly as the client sent it, so the offset the client supplied determines the result. The source comment says UTC, but the code does not convert to UTC.

Verified by calling the function directly: 10:00–11:00 IST costs 500; 17:00–18:00 IST costs 800; 22:00–23:00 IST costs 800, because the peak rule has no end time; 16:30–17:30 IST costs 500, even though it overlaps 17:00; 17:00–18:00 UTC (22:30 IST) costs 800.

The earlier design document described peak as 17:00–21:00. That is not what the code does. Venue timezone is not used. The pricing rule is a sample policy and is expected to be replaced (section 13).

### Confirmation

`confirm` locks the reservation row, checks that the member matches and that the hold has not expired (against the application clock), sets the row to `confirmed`, and creates a payment for the reservation's stored price with status `succeeded`. The payment's idempotency key is `confirm:` followed by the caller's key. A retry with the same key on the same reservation returns the existing payment. The intended guarantee is per reservation, but the uniqueness check is global, which causes defect D2.

### Cancellation

Only `confirmed` bookings can be cancelled. A cancellation at least 24 hours before start refunds the full stored price. A cancellation within 24 hours refunds zero. Both cases create a `payment_cancellations` row, and the reservation moves to `cancelled`, which releases the slot. The refund calls the provider adapter with the key `cancel:` followed by the caller's key.

If the booking is already cancelled, the endpoint returns the refund recorded under the same key. A request with a different key returns zero, even if the original cancellation refunded the full amount (defect D16).

### Recurring weekly series

`POST /recurring/weekly` creates one confirmed booking for each of N weeks, spaced seven days apart. All rows are inserted in one transaction, so any conflict rolls back the whole series with a 409. The member-limit check counts the requested number of weeks against existing future bookings. Because the limit is three, a series can never exceed three weeks in practice, although validation accepts 1 to 52.

Recurring bookings do not pass through the payment service. This makes them uncancellable through the cancel endpoint (defect D3).

### Availability

`GET /courts/{id}/availability` expires elapsed holds, then reports whether any `held` or `confirmed` row overlaps the requested interval. It returns 404 for missing or inactive courts. The answer is a snapshot. The hold endpoint is the authoritative attempt to reserve. The endpoint writes to the database because it runs the expiry sweep (defect D13).

### Utilization

`GET /staff/utilization/weekly` returns, for each court (optionally filtered to one venue), the sum of hours of `confirmed` reservations that overlap a seven-day window starting at `week_start`. It excludes `held` rows. "Utilization" here means booked hours, not a percentage, because the model has no opening hours and therefore no capacity figure. The window is computed from the supplied `week_start` and is not aligned to venue-local time. The report includes inactive courts (reproduced).

## 5. Correctness guarantees

| Guarantee | Mechanism | Status |
|---|---|---|
| No overlapping active reservations on a court | PostgreSQL exclusion constraint with `btree_gist`. The database is the final authority; the availability check is only advisory. | Enforced. Covered by `test_database_rejects_overlapping_active_reservations`. The service converts any `IntegrityError` at flush into "Court slot is unavailable", so other integrity failures can be reported the same way. |
| Confirm retries do not double charge | Unique payment key and a row lock on the reservation during confirmation. | Enforced for one reservation. Not enforced across reservations (D2). |
| Multi-step database changes are atomic | Each service operation runs in one transaction, or in a savepoint when the caller already has one. Constraint errors roll back. | Enforced for database work. External provider calls happen inside the transaction and cannot be rolled back (D9). |
| Holds are finite | Each hold stores an expiry timestamp. Confirmation rejects expired holds. | Enforced by the application clock. Not tested. A cleanup worker cannot extend a hold because no worker exists. |
| Member limit of three | Count-then-insert with no lock. | **Not guaranteed** under concurrency (D8). |

## 6. Time handling

Stored times are `timestamptz`. The application compares them with `datetime.now(UTC)`, which is the application server's clock. The database clock is not used for expiry or for the future-start check. If several application instances run with clock skew, a hold can expire earlier or later than its nominal five minutes by the size of the skew. Time comparisons assume the server clock is synchronized, for example with NTP.

Pricing and the utilization window read the wall-clock values of the datetimes supplied by the client, so the offset in each request matters. Venue timezone is stored but unused.

## 7. API contract

Errors use FastAPI's `{"detail": ...}` body. There are no machine-readable error codes. Money values are returned as decimal strings, for example `"500.00"`.

| Method and path | Input | Success | Error responses |
|---|---|---|---|
| `POST /holds` | JSON: `court_id`, `member_id`, `starts_at`, `ends_at`, `idempotency_key` (8–200 characters) | 201 with the hold | 400 for naive, past, or inverted intervals; 404 for missing or inactive court; 409 for unavailable slot, member at limit, or key belonging to another member; 422 for schema violations |
| `POST /holds/{id}/confirm` | JSON: `member_id`, `idempotency_key` | 200 with the payment | 404 when the hold is missing or belongs to another member; 409 when expired or not confirmable |
| `POST /bookings/{id}/cancel` | JSON: `member_id`, `idempotency_key` | 200 with `booking_id`, `status`, `refund_amount`, `currency` | 404 when missing or wrong member; 409 when not confirmed; 500 "Payment record missing" for recurring bookings |
| `GET /courts/{id}/availability` | Query: `starts_at`, `ends_at` (timezone-aware) | 200 with `available` | 404 for missing or inactive court; 422 for naive or inverted interval |
| `POST /recurring/weekly` | Query: `court_id`, `member_id`, `starts_at`, `ends_at`, `weeks` (1–52). Header: `Idempotency-Key` (≥8 characters) | 200 with `created` and `booking_ids` | 409 for limit or conflict; 404 for missing court; 422 for naive or inverted interval |
| `GET /staff/utilization/weekly` | Query: `week_start` (timezone-aware), optional `venue_id` | 200 with a list of court rows | 422 for naive `week_start` |
| `GET /health` | None | 200 `{"status": "ok"}` | None |

The idempotency key on recurring requests is stored, but it is only used to build per-row keys and is never looked up before the request runs (D5). A hold replay with the same key returns 201 for the original hold, not 200, because the route always uses 201.

Authentication is absent. Member identity is supplied in request bodies or query strings, and the utilization endpoint requires no credentials at all.

## 8. Payment boundary

`PaymentProvider` is an adapter with two methods, `charge` and `refund`. The mock returns deterministic strings such as `mock-charge:<key>` and never fails. `PaymentService` creates payment rows, and `PaymentCancellation` creates cancellation records and applies the refund policy. The module docstring says a real provider can replace the mock without changing routes. That is true for routes, but not for transactions: `charge` and `refund` run inside the database transaction that holds the reservation's row lock. A real provider that succeeds followed by a database failure would leave money moved and the database rolled back. Section 13 covers the fix.

## 9. Configuration

Settings load from the environment and from `.env`, using pydantic-settings. The fields are `DATABASE_URL` (default points at localhost with the `courtbook` user), `HOLD_TTL_SECONDS` (default 300), `REDIS_ENABLED` and `REDIS_URL` (unused by code), and `APP_ENV` (unused by code). The `.env.example` file points at the hostnames `db` and `redis`, which resolve only inside a Compose bridge network. The Compose file uses host networking, so `127.0.0.1` is the correct host there.

## 10. Schema management

On every startup, the application runs `CREATE EXTENSION IF NOT EXISTS btree_gist` and `Base.metadata.create_all`. `create_all` creates missing tables but never alters existing ones. The Alembic revision `0001_initial` also calls `create_all` against the current models, so it does not reproduce a historical schema, and its downgrade drops every table. Before any deployed schema changes, the project must move to revisions that contain explicit DDL and must stop calling `create_all` at startup.

## 11. Deployment and operations

`docker compose up --build` starts PostgreSQL 15 and the API using host networking, which is intended for Linux Docker Engine. Ports 5432 and 8000 are bound on the host. The database password is hard-coded in `docker-compose.yml`. The API image runs as root, has no health check, and has no `.dockerignore`, so `.env` and `.git` would be copied into the image by `COPY . .`. The README says host networking also applies to the image build; the Compose file does not set `build.network`, so that claim should be verified before relying on it.

Dependencies are declared in two places, requirements.txt and uv.lock, and pyproject.toml declares no dependencies. The Python version is inconsistent: the Dockerfile and README use 3.11, CI and mypy use 3.14, uv.lock requires 3.14, and five `.pyc` files for CPython 3.13 are tracked in git despite `.gitignore` excluding them.

CI runs ruff lint, ruff format check, mypy, and pytest against PostgreSQL 15. The workflow lists the pytest step twice.

## 12. Known defects

The table below lists defects found in review. "Reproduced" means the behavior was observed through the API. "By reading" means it follows from the code.

| ID | Area | Behavior | Evidence | Severity |
|---|---|---|---|---|
| D2 | Confirm | Reusing one confirm key on two reservations returns the first reservation's payment for the second. The second reservation becomes `confirmed` with no payment of its own. | Reproduced: second confirm returned the first reservation's `reservation_id`. | High |
| D3 | Recurring | Recurring bookings have no payment record. Cancelling one returns 500 "Payment record missing". | Reproduced. | High |
| D4 | Recurring | Recurring requests accept inactive courts and start times in the past. | Reproduced: both returned 200. | High |
| D5 | Recurring | The `Idempotency-Key` header is never looked up. An identical retry returns 409, and the message "would exceed the 3-booking limit" is misleading. | Reproduced. | Medium |
| D6 | Holds | Replaying a hold with the same key but a different time returns the original hold with 201. The caller is not told the new request was ignored. | Reproduced: returned the original start time. | Medium |
| D8 | Member limit | Concurrent requests from one member can exceed three active bookings. | By reading; acknowledged in LIMITATIONS.md. | Medium |
| D9 | Payments | Provider calls run inside the database transaction and cannot be rolled back. | By reading; acknowledged in LIMITATIONS.md. | High once a real provider is used |
| D10 | Pricing | Peak pricing has no 21:00 end, uses the client's offset, and treats 16:30–17:30 as off-peak. The earlier document described a different rule. | Reproduced by calling `price_for` directly. | Medium |
| D12 | Utilization | The staff endpoint needs no credentials and includes inactive courts. | Reproduced. | High for exposure |
| D13 | Availability | A GET request writes to the database to expire holds. | By reading. | Low |
| D14 | Tests | The `db_session` fixture deletes every row in every table during teardown. Running the tests against a non-disposable database would erase its data. | By reading. | High operational risk |
| D15 | Integrity errors | Any `IntegrityError` during hold creation, including a duplicate idempotency key, is reported as "Court slot is unavailable". | By reading. | Low |
| D16 | Cancellation | Cancelling an already-cancelled booking with a new key returns a zero refund, even if the original cancellation refunded the full amount. | By reading. | Low |
| D17 | Security | Member identity comes from the caller. Any client can act as any member. | By reading; acknowledged in LIMITATIONS.md. | High before exposure |

Test coverage is thin. The `test_contract.py` file contains one pricing unit test, not a contract test. Three database tests cover the exclusion constraint, the confirm retry, and rollback on failure. No test exercises the HTTP routes, although httpx is a dependency, and nothing covers cancellation, recurring series, availability, utilization, or hold expiry.

## 13. Open questions and decisions

Several defects depend on product decisions. Should unconfirmed holds count toward the three-booking limit? The code says yes; the earlier document said no. Should recurring series be paid up front, charged per occurrence, or free? Who is responsible for the three-booking limit when a series is created? What is the peak schedule, does it have an end time, and does it belong to each venue? Should pricing use venue-local time, and can staff change prices over time? Should inactive courts accept recurring bookings? What counts as utilization: booked hours or a percentage of opening hours, and where are opening hours stored? Which identity provider and roles apply, and who may cancel or override a booking? Which payment provider, currencies, tax rules, fees, and refund settlement apply? What are expected request volume, retention, data residency, and availability targets?

## 14. Alternatives considered

Redis locks as the booking authority were rejected because lock expiry, network partitions, and failover can break correctness. Redis may later accelerate reads, but PostgreSQL constraints remain the authority.

A preflight availability check followed by an insert was rejected because two concurrent requests can both see an empty slot. The exclusion constraint closes that race.

Deleting expired holds immediately was rejected because it loses history. Holds move to `expired` and are kept.

Putting booking logic in one route handler was rejected in favor of services and a payment adapter. The recurring endpoint currently departs from this and should be moved into the service layer.

A distributed transaction across PostgreSQL and a payment provider is generally not available. A real integration needs provider idempotency keys, an outbox, reconciliation for unknown outcomes, and compensating actions when the provider and database disagree.

## 15. Recommended order of work

Fix D2, D3, and D4 first, because they break money and booking rules in normal use. Then fix D5 and D6 so retries behave predictably, and move recurring logic into `BookingService`. Protect the staff endpoint and move member identity onto verified authentication (D12, D17). Make the test fixture use a disposable database and add HTTP tests for each route (D14). Before a real provider is connected, move provider calls outside the database transaction with an outbox and reconciliation (D9). Resolve the concurrency gap on the member limit with a row lock on the member or a counter table (D8). Replace `create_all` with Alembic revisions that contain explicit DDL (section 10).
