

# Limitations, Risks, and Target Mitigations

> **Status:** This file describes current limitations and planned mitigations. A mitigation is not implemented until the corresponding code and tests are merged.

## Critical: Do Not Expose or Process Real Payments

- **Payment adapter is a mock.** It returns synthetic references and does not contact a provider. Never accept real money with it.
- **External side effects are inside database transactions.** A real provider can complete a charge/refund while the database transaction later rolls back. Target: persist payment intent and an outbox event in one transaction; call the provider from a worker outside the database transaction; use provider idempotency, signature-verified webhooks, reconciliation for unknown outcomes, and compensating actions.
- **Recurring bookings have no payment record.** They bypass the payment workflow and cannot be cancelled through the normal cancellation path. Decide upfront versus per-occurrence billing and implement explicit series and refund states.
- **Authentication is absent.** `member_id` comes from the caller. This is not safe to expose publicly. Target: validate OIDC/JWT, map the verified subject to an internal member, derive ownership from the authenticated principal, and enforce staff roles.
- **Staff utilization is unprotected.** Add staff authorization and decide how inactive courts should be reported.

## Booking Correctness and Concurrency

- **Confirm idempotency can cross reservation boundaries.** A globally unique key can return another reservation's payment. Scope idempotency to member + operation + resource, persist a request fingerprint and result, and test key reuse against a different reservation.
- **Hold replay can silently ignore changed input.** Reusing a key with a different interval must return a conflict, not the original hold.
- **Recurring idempotency is not looked up.** Persist and return the original series for an identical retry.
- **Recurring validation is incomplete.** Reject inactive courts, past starts and invalid intervals through shared BookingService policies.
- **Member limit races.** Unlocked count-then-insert can exceed three under concurrent requests. Add a member row and lock it during limit checks and booking writes. Test with separate database connections.
- **Pricing is a sample policy.** It uses the caller-supplied offset and an incomplete peak rule. Decide the peak schedule, venue-local timezone, holiday rules, currency/taxes and price locking. Version the policy and persist the applied rule.
- **Availability currently writes during GET.** Make availability read-only. Expiry cleanup belongs in write workflows or a worker; elapsed holds must not block booking correctness.
- **Integrity errors are over-generalized.** Only the exclusion-constraint violation should map to "slot unavailable". Map other database constraints to their own domain errors.
- **Repeated cancellation with a new key can return the wrong refund.** Return the persisted cancellation outcome and define idempotency semantics for already-cancelled bookings.

## Data and Test Safety

- **Test teardown can delete every row in the configured database.** Never run it against a developer or production database. Fail closed unless the URL explicitly identifies a disposable test database. Use isolated transactions or an ephemeral test database. Concurrency tests need independent connections.
- **Route test coverage is thin.** Add HTTP tests for every route, ownership/authz, error cases, expiry, cancellation, recurring workflows, utilization and retries.
- **PostgreSQL is the booking authority.** Preserve the GiST exclusion constraint. Do not move correctness to Redis or rely on an availability preflight query.
- **Schema startup uses `create_all`.** It creates missing tables but does not migrate existing tables. Replace it with explicit Alembic revisions and run migrations as a deployment step. The runtime app role should not need schema-owner privileges.

## Runtime and Operations

- **Python version configuration must agree.** README and Dockerfile specify Python 3.11; CI is now set to 3.11, but mypy's `python_version` and lockfile/runtime metadata must also be aligned. Validate one supported interpreter across local, CI and Docker.
- **Redis is configured but unused.** Do not add it to booking correctness. Introduce it only for measured caching or rate limiting, with TTL, invalidation, bounded memory and PostgreSQL fallback.
- **Host networking is Linux-specific.** Document port exposure and isolation or move to a bridge network and service DNS.
- **Secrets and container hardening are incomplete.** Remove hard-coded passwords, keep `.env` and `.git` out of images, add `.dockerignore` and run the API as non-root.
- **Health/observability are incomplete.** Add liveness/readiness checks, structured logs, metrics, tracing, request IDs and error reporting.
- **Recovery is unproven.** Add backup retention, restore drills and point-in-time recovery procedures.
- **Capacity and governance are undefined.** Define load targets, retention, data residency and availability objectives. Test under expected concurrency.

## Architecture Direction

Keep CourtBook a modular monolith.

Centralize all booking use cases in application services, keep routes thin, preserve PostgreSQL constraints as the correctness boundary, and use a database-backed transactional outbox plus worker for external payment operations.

Do not introduce microservices or make Redis a booking-lock authority without a demonstrated operational need.
