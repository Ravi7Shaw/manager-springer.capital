# Limitations and failure modes

- **Payment adapter is a mock.** Do not accept real money with it. A real provider can accept a charge/refund while the database transaction later fails. Add provider idempotency keys, webhook signature verification, a transactional outbox, and reconciliation for unknown outcomes.
- **Authentication is not implemented.** `member_id` is supplied by the caller, so this is not safe to expose publicly. Add OIDC/JWT, role checks, and ownership derived from the authenticated identity.
- **Pricing is a sample policy.** It uses UTC hour rather than venue-local timezone and does not support holiday pricing, taxes, currency conversion, or rule versioning.
- **Recurring bookings bypass payment creation** and currently create confirmed rows directly. Before production, decide whether the entire series is paid upfront or charged occurrence-by-occurrence and implement the same idempotent payment workflow for the chosen policy.
- **Three-booking limit needs a concurrency-safe member-level lock or database enforcement.** Current count checking alone can race when a member confirms/creates multiple bookings concurrently. Use a member row lock or a dedicated per-member counter/constraint strategy.
- **Hold cleanup is request-driven.** This is sufficient to make expired holds stop blocking new operations, but an operational cleanup worker is still useful for storage hygiene and alerting.
- **Redis is configured but not used yet.** That is intentional: no critical workflow depends on it. Enable it only for cache/rate-limit features with PostgreSQL fallback.
- **Availability is a snapshot.** A slot can become unavailable immediately after the response. The hold endpoint is the authoritative attempt to reserve it.
- **No production observability yet.** Add structured logs, metrics, tracing, error reporting, database backups/PITR, migration gates, secret management, and load tests before production.
- **Schema bootstrap uses `create_all` for this starter.** Use Alembic revisions for all deployed schema changes; never rely on `create_all` as a production migration strategy.
