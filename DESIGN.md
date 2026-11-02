

# CourtBook Design

> **Document status:** Architecture baseline and target design.
>
> This document distinguishes current implementation from planned behavior. A recommendation does not mean the corresponding code has already been implemented.

## 1. Purpose and Scope

CourtBook is a FastAPI service for booking courts at a venue. It supports temporary court holds, confirmation, cancellation and refunds, weekly recurring bookings, availability checks, and staff utilization reporting.

This is a learning and engineering starter, not a production system. Authentication is not implemented and the payment provider is a mock. Do not expose the service publicly or accept real money until the security, payment, correctness, and operational work described here is implemented and tested.

## 2. Architecture

### 2.1 Current Architecture

The current application is a **modular monolith**:

- HTTP routes and error mapping: `app/main.py`
- Request and response schemas: `app/schemas.py`
- Booking rules and transactions: `app/services/bookings.py`
- Payment service and provider adapter: `app/services/payments.py`
- SQLAlchemy models: `app/models/entities.py`
- Database engine and session factory: `app/db.py`
- PostgreSQL is the system of record.
- Redis is configured but currently unused by request paths.

Most routes delegate to `BookingService`. The recurring-booking route currently contains its own business logic. This inconsistency is a source of defects and must be removed.

### 2.2 Target Architecture

Keep CourtBook as a **modular monolith** for the foreseeable future.

The current domain does not justify splitting booking, payments, or venues into microservices. Premature service separation would introduce network failure modes, deployment overhead, and distributed consistency problems.

Use explicit boundaries:

| Module | Responsibility |
|---|---|
| API layer | HTTP parsing, authentication dependencies, status codes, response schemas |
| Booking application service | Hold, confirm, cancel, recurring-booking use cases |
| Booking domain policies | Reservation states, interval rules, member limits, pricing policies |
| Payment application service | Payment/refund state machine and idempotency |
| Payment provider adapter | Provider-specific API calls, signatures and references |
| Persistence layer | SQLAlchemy sessions, repositories and database constraints |
| Worker | Outbox delivery, payment reconciliation and optional expiry cleanup |
| Operations | Logs, metrics, tracing, health checks, backups and migrations |

Routes must remain thin. Every booking path, including recurring bookings, must use the same application-service boundary.

Services must not depend on FastAPI request objects or HTTP status codes.

Use dependency injection for service construction and a request-scoped SQLAlchemy session.

Transaction ownership must be explicit. Application-service use cases own their units of work. Repositories must not commit independently.

### 2.3 Target Request Flow

For a booking mutation:

1. Authenticate the caller.
2. Derive the member ID from the verified identity.
3. Validate the request and require timezone-aware timestamps.
4. Invoke the relevant application service.
5. Execute database changes in one transaction.
6. Let PostgreSQL enforce concurrency-sensitive invariants.
7. Persist external work as an outbox event in the same transaction.
8. Commit and return the committed resource.

A worker processes outbox events and calls external providers using stable provider idempotency keys.

Webhooks must be signature-verified and processed idempotently. Unknown payment outcomes must be reconciled rather than guessed.

## 3. Data Model

### 3.1 Current Tables

| Table | Purpose |
|---|---|
| `venues` | Venue name and timezone |
| `courts` | Courts belonging to venues |
| `reservations` | Court, member UUID, interval, status, hold expiry and price |
| `payments` | Payment records |
| `payment_cancellations` | Refund and cancellation records |

There is currently no members table. `member_id` is supplied by the caller and is not a verified identity.

The current schema also has no outbox, payment-attempt, recurring-series, or opening-hours model.

### 3.2 Target Data Model Changes

Introduce a `members` or identity-mapping table if CourtBook owns member profiles.

Store the external identity-provider subject as a unique, immutable identifier. Reservations should reference the internal member ID using a foreign key.

For booking-limit enforcement, lock the member row or a dedicated member-booking-account row.

After acquiring the lock, count eligible reservations and create the booking within the same transaction.

Add an `outbox_events` table with:

- Event ID
- Aggregate type and aggregate ID
- Event type and schema version
- Payload
- Creation and availability timestamps
- Attempt count
- Last error
- Lock/lease metadata
- Processed timestamp

Add indexes for ready-event polling.

Keep secrets and unnecessary personal data out of event payloads.

If recurring bookings are a first-class feature, introduce a recurring-series record linking generated reservations and recording the selected billing policy.

## 4. Booking Rules and Concurrency

### 4.1 Intervals and Database Authority

Booking intervals are timezone-aware instants stored as PostgreSQL `timestamptz`.

Reservation intervals are half-open:

`[starts_at, ends_at)`

Adjacent intervals such as 10:00–11:00 and 11:00–12:00 do not overlap.

The GiST exclusion constraint on court equality and timestamp-range overlap is the authoritative protection against double booking.

**Preserve this constraint.**

Availability reads are advisory snapshots. A successful hold insert is the authoritative reservation attempt.

Do not replace this constraint with Redis locks or a preflight query.

PostgreSQL remains the source of truth for booking conflicts.

### 4.2 Reservation State Machine

Current states:

- `held`: temporary reservation with an expiry timestamp.
- `confirmed`: confirmed reservation.
- `expired`: a hold whose expiry was observed.
- `cancelled`: a confirmed booking that was cancelled.

The current recurring endpoint bypasses the normal hold/confirmation/payment lifecycle and inserts confirmed rows directly.

Target: define legal state transitions centrally and enforce them in the application service.

Preserve historical rows. Do not delete expired or cancelled reservations to release a slot.

### 4.3 Holds and Expiry

The current hold TTL defaults to 300 seconds.

Expiry is request-driven. There is no background worker.

Target:

- Use one documented clock policy for expiry and future-start checks.
- Prefer database time for decisions that must be consistent across application instances.
- A cleanup worker may perform storage hygiene and alerting.
- Correctness must not depend on the worker running.
- Confirmation must still reject expired holds.

### 4.4 Member Booking Limit

Current behavior counts future `held` and `confirmed` reservations toward a limit of three.

Product owners must explicitly decide whether unconfirmed holds count.

The current count-then-insert logic is not concurrency-safe.

Target:

1. Authenticate the member.
2. Lock the member's booking-account row.
3. Count eligible reservations.
4. Validate the limit.
5. Create the booking within the same transaction.

All booking paths, including recurring creation and confirmation, must use the same enforcement function.

Add concurrent tests proving the limit cannot be exceeded.

## 5. Idempotency

An idempotency key identifies a logical operation. It is not merely a string to put in a globally unique column.

Target requirements:

- Scope keys by authenticated member, operation and resource/request context.
- Store a request fingerprint.
- Store the final response or resource reference.
- Same key and same logical request returns the original result.
- Same key and different payload returns a conflict.
- Enforce uniqueness in PostgreSQL.
- Use stable provider idempotency keys for external operations.
- Never return a payment belonging to another reservation.

Fix the current confirmation defect where a globally unique key can cause reservation B to receive reservation A's payment.

Fix hold replay behavior where a reused key with a different interval silently returns the original hold.

Recurring requests must perform idempotency lookup before doing booking work.

## 6. Payments and Refunds

### 6.1 Current Behavior

The provider adapter is a mock that returns synthetic references.

Current charge/refund calls execute inside the database transaction.

This is acceptable only as a mock demonstration. A real provider side effect cannot be rolled back by PostgreSQL.

Recurring reservations currently have no payment record, so the normal cancellation path fails for them.

### 6.2 Target Payment Workflow

Do not call a real payment provider while holding a database transaction open.

Use a persisted payment state machine and transactional outbox:

1. In a database transaction, validate and lock the reservation.
2. Create a payment intent or attempt.
3. Create an outbox event.
4. Commit the transaction.
5. A worker claims the event.
6. The worker calls the provider outside the database transaction.
7. Use a stable provider idempotency key.
8. Persist the provider result in a new transaction.
9. Verify provider webhooks and apply them idempotently.
10. Reconcile unknown outcomes by querying the provider.
11. Use compensating actions for inconsistent outcomes.

Refunds follow the same pattern.

Store refund attempts and provider references. Cancellation records should distinguish:

- requested
- pending
- succeeded
- failed
- unknown

Do not represent every refund request as immediately completed.

### 6.3 Recurring Billing Decision

Before implementing recurring payments, decide whether a series is:

- Paid in full before any occurrence is confirmed.
- Charged per occurrence.

The current behavior of creating confirmed reservations with no payments is not an acceptable production policy.

Define recurring cancellation and partial-series cancellation semantics explicitly.

## 7. Pricing and Time Zones

Current pricing is a sample rule: INR 500 or INR 800 based on the supplied datetime's hour.

It does not convert to the venue timezone, does not implement the earlier-described 17:00–21:00 interval, and mishandles intervals crossing the peak boundary.

Target:

- Store booking instants as aware `timestamptz` values.
- Load the venue's IANA timezone.
- Evaluate pricing windows in venue-local time.
- Define peak windows with explicit start and end.
- Handle cross-midnight windows if required.
- Detect interval overlap with the pricing window.
- Version pricing rules.
- Persist the applied rule/version and final amount.
- Define currency, taxes, fees, rounding and price locking.

The exact peak schedule, holiday rules and price-locking policy are product decisions.

Do not invent them in code.

## 8. Availability and Utilization

Availability is a snapshot, not a reservation.

Keep the GET endpoint read-only. It should not mutate reservation states.

Expired holds must not be treated as valid availability blockers.

The hold command remains authoritative, and the exclusion constraint handles the final race.

Current utilization reports confirmed booked hours over a supplied seven-day window.

It is not a percentage because opening hours and capacity are not modeled.

Decide whether inactive courts should appear and whether the report uses venue-local calendar weeks.

If percentage utilization is required, model court opening hours and closures.

## 9. Authentication and Authorization

The current API trusts `member_id` supplied in request bodies and query parameters.

The staff utilization route has no authorization.

Target:

- Integrate an OIDC identity provider.
- Validate JWT signature, issuer, audience, expiry and permitted algorithms.
- Map the verified subject to an internal member record.
- Derive member identity from the authenticated principal.
- Do not accept caller-selected member IDs for ownership-sensitive operations.
- Enforce ownership checks in service-level authorization rules.
- Add explicit staff roles and permissions.
- Deny access by default.

Test cross-member access, privilege escalation, expired tokens and invalid tokens.

The identity provider and staff permissions remain product/security decisions.

## 10. API Contract and Error Model

Current endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health response |
| POST | `/holds` | Create temporary hold |
| POST | `/holds/{id}/confirm` | Confirm hold |
| POST | `/bookings/{id}/cancel` | Cancel booking |
| GET | `/courts/{id}/availability` | Availability snapshot |
| POST | `/recurring/weekly` | Create weekly series |
| GET | `/staff/utilization/weekly` | Staff utilization |

Current errors use FastAPI's `{"detail": ...}` shape.

Target a consistent problem response containing:

- Stable error code
- Human-readable detail
- Optional field errors
- Request/correlation ID

Validation errors, not-found, conflict and unexpected internal errors must remain distinguishable.

Never map every SQLAlchemy `IntegrityError` to "slot unavailable".

Inspect the violated constraint or SQLSTATE and map only the exclusion-constraint violation to a booking conflict.

Do not expose raw database details to clients.

## 11. Transactions and Persistence

Keep transaction ownership explicit at the application-service use-case boundary.

A single use case should commit all its database state changes atomically.

Avoid hidden commits inside repositories.

The current service may use savepoints when a session is already in a transaction.

A savepoint is not an independent commit.

Use database constraints for invariants that must survive concurrency:

- Interval validity
- Active-slot exclusion
- Foreign keys
- Uniqueness
- Valid states

Application validation improves error messages but does not replace database enforcement.

## 12. Schema Migrations

Current startup runs `CREATE EXTENSION IF NOT EXISTS btree_gist` and `Base.metadata.create_all`.

The initial Alembic revision also delegates to current metadata and its downgrade drops all tables.

Target:

- Stop creating application tables at service startup.
- Use explicit, ordered Alembic revisions containing DDL.
- Make extension creation and required privileges an explicit migration concern.
- Test fresh database installation.
- Test upgrades from the previous schema.
- Run migrations as a deployment step.
- Use a controlled database role for migrations.
- Keep schema-owner privileges away from the runtime application role.
- Make destructive downgrades explicit and safe.

## 13. Redis and Background Workers

Redis is currently configured but unused.

Do not add Redis to the booking correctness path.

Introduce Redis only for measured use cases such as:

- Short-lived read caching.
- Rate limiting.

Every cache needs a TTL, invalidation strategy, bounded memory and safe PostgreSQL fallback.

Cache data must never override the exclusion constraint.

Use a worker for:

- Outbox delivery.
- Payment reconciliation.
- Optional hold cleanup.

For the current scale, a database-backed outbox with PostgreSQL polling is sufficient.

Add a message broker only when throughput, fan-out or operational needs justify it.

## 14. Testing Strategy

Current tests cover only a small subset of behavior.

The database fixture can delete all rows in its configured database.

Target test layers:

- Unit tests for pricing, interval policies, state transitions and idempotency fingerprints.
- Service tests using a dedicated disposable PostgreSQL database.
- HTTP/API tests for every route.
- Authentication and authorization tests.
- Database integration tests for exclusion constraints, foreign keys, uniqueness and transaction rollback.
- Migration upgrade tests.
- Concurrency tests using separate database connections.
- Payment adapter tests for success, failure, timeout and unknown outcomes.
- Webhook duplicate-delivery tests.
- Worker retry and lease-expiry tests.

The test fixture must fail closed unless the database is explicitly identified as disposable.

Never run teardown-wide deletes against a developer or production database.

Prefer transaction rollback where compatible. Use a dedicated ephemeral database for tests that must commit or exercise concurrency.

## 15. Deployment and Operations

Before production:

- Remove hard-coded credentials.
- Use environment-injected secrets or a secret manager.
- Add `.dockerignore`.
- Ensure `.env`, VCS metadata, caches and virtual environments are not copied into images.
- Run the API as a non-root user.
- Add container health checks.
- Separate liveness and readiness checks.
- Document the Linux host-network requirement or move to a bridge network.
- Add structured logs, metrics, traces, request IDs and error reporting.
- Define backup retention.
- Regularly test restore and PITR.
- Add migration gates.
- Add load tests and resource limits.
- Define data retention, privacy, data residency and availability targets.

## 16. Runtime and CI Configuration

The supported runtime must be consistent across local development, Docker, CI and type checking.

The README and Dockerfile specify Python 3.11, and the CI workflow has been changed to Python 3.11.

The current `pyproject.toml` must also configure mypy for Python 3.11.

The lockfile/runtime metadata must be regenerated or updated consistently rather than mixing interpreter targets.

CI should run:

1. Ruff lint.
2. Ruff format check.
3. Mypy.
4. Pytest.

Each check should run once against the documented PostgreSQL version.

Keep the test database dedicated to CI.

## 17. Known Defects and Planned Resolution

| ID | Current defect | Target resolution |
|---|---|---|
| D2 | Confirm idempotency can return another reservation's payment | Scope operation identity to member, operation and reservation; persist request fingerprint/result |
| D3 | Recurring bookings have no payment record | Choose series billing policy and implement payment workflow |
| D4 | Recurring accepts inactive courts and past starts | Move workflow into BookingService and apply shared validation |
| D5 | Recurring key is not checked | Persist and return original series for identical retry |
| D6 | Hold replay with changed interval silently returns original hold | Compare request fingerprint; return conflict for changed input |
| D8 | Member limit races | Add member row and lock it during checking and booking writes |
| D9 | Provider call occurs inside DB transaction | Outbox, worker, provider idempotency, webhooks and reconciliation |
| D10 | Pricing uses caller offset and incomplete peak rule | Venue-local interval-overlap pricing with versioned rules |
| D12 | Staff utilization is unauthenticated | Add staff authorization |
| D13 | Availability GET mutates database | Make GET read-only and move cleanup to worker/write workflow |
| D14 | Test fixture deletes all rows | Dedicated disposable test database and fail-closed URL guard |
| D15 | Every IntegrityError maps to slot conflict | Inspect constraint/SQLSTATE and map only exclusion violations |
| D16 | New-key repeat cancellation returns wrong refund | Return persisted cancellation outcome |
| D17 | Caller supplies member identity | OIDC/JWT principal mapped to internal member |

Additional gaps: route-level tests are absent, hold expiry is not tested, utilization includes inactive courts, and runtime configuration must remain consistent.

## 18. Decisions Required Before Implementation

Do not silently choose business policy for these:

1. Do unconfirmed holds count toward the three-booking limit?
2. Are recurring series paid upfront or per occurrence?
3. What happens when one occurrence in a paid series is cancelled?
4. What is the exact peak pricing window?
5. Which venue-local timezone and holiday rules apply?
6. Can staff override or cancel a member's booking?
7. Should inactive courts appear in utilization reports?
8. Is utilization booked hours or a percentage of opening capacity?
9. Which OIDC provider and staff roles are required?
10. Which payment provider, currencies, fees, taxes and refund rules apply?
11. What are the expected request volume, retention, data residency and availability objectives?

## 19. Recommended Implementation Sequence

1. **Safety baseline:** dedicated test database guard, tests for every route, align Python configuration and remove duplicate CI steps.
2. **Booking correctness:** centralize recurring booking logic, fix D2–D6, D10, D13, D15 and D16, and add concurrency-safe member limits.
3. **Identity and authorization:** members/identity mapping, OIDC/JWT, ownership and staff roles.
4. **Schema lifecycle:** explicit Alembic migrations and removal of startup `create_all`.
5. **Payment reliability:** billing policies, payment state machine, outbox worker, signed webhooks and reconciliation.
6. **Operations:** secrets, non-root image, health checks, observability, backup/PITR and load tests.

Keep the application a modular monolith through these phases.

Split components into separate services only when independent scaling, ownership or deployment needs are demonstrated.
