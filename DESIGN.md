# CourtBook design decisions

## Scope and assumptions

- All booking intervals are timezone-aware instants; the API stores `timestamptz` and uses half-open intervals `[start, end)`, so 10:00–11:00 and 11:00–12:00 do not overlap.
- A hold lasts five minutes by default. Expired holds stop blocking when a booking operation expires them; a worker may clean up old rows but correctness does not depend on worker timing.
- A member is identified by `member_id` for this initial API. Production deployment must add authentication and derive the member ID from a verified principal, not trust a request body.
- Up to three confirmed future bookings are allowed per member. Holds do not count toward this limit in the first iteration; this should be clarified with the client.
- Full refund applies when cancellation occurs at least 24 hours before start; otherwise the refund is zero.
- Pricing currently uses a documented default rule (INR 500 off-peak, INR 800 peak, 17:00–21:00). Venue-local timezone and configurable pricing schedules are a follow-up requirement before production.
- Payment provider is a mock adapter. The data model and service boundary are designed for a real provider integration.
- Weekly recurrence creates all requested occurrences atomically. If any occurrence conflicts, none are created.
- Redis is optional and is not part of the correctness path.

## Guarantees and how they are enforced

1. **No overlapping active reservations:** PostgreSQL `EXCLUDE USING gist (court_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&)` with `btree_gist` rejects overlapping active held/confirmed rows. The database, not a preflight availability query or Redis lock, is the final authority.
2. **Safe confirm retries:** `Payment.idempotency_key` is unique and confirmation locks the reservation row. Repeating a confirmation with the same key returns the persisted payment rather than creating another.
3. **Atomic multi-step database changes:** hold creation, confirmation plus payment record, cancellation plus refund record, and weekly series creation each run inside one database transaction. Constraint errors roll back the transaction.
4. **Finite holds:** the hold has an explicit expiry timestamp. Every hold-creation and confirmation transaction expires elapsed holds before continuing; a delayed cleanup worker cannot extend the hold lifetime.

## Questions for the client

- What identity provider and member/staff roles are required?
- Are bookings fixed-duration slots or arbitrary intervals? What are court opening hours and buffers between sessions?
- Are venue peak schedules local to each venue, and can staff change prices over time?
- Should temporary holds count toward the three-booking limit? Should all occurrences in a recurring series count?
- Does a recurring series need all-or-nothing behavior, or should valid weeks succeed independently?
- Which payment provider, currencies, tax rules, fees, and refund settlement semantics apply?
- Can staff override bookings, and what audit trail is required?
- What are expected request volume, retention, data residency, and availability targets?

## Alternatives rejected

- **Redis locks as the booking authority:** rejected because lock expiry, network partitions, and failover can undermine correctness. Redis may accelerate reads but PostgreSQL constraints enforce no-overlap.
- **Availability check then insert only:** rejected because two concurrent requests can both observe an empty slot. The exclusion constraint closes this race.
- **Delete expired holds immediately:** rejected because deletion loses audit history. Holds transition to `expired` and are retained.
- **One giant route with business logic:** rejected in favor of services and a payment provider adapter.
- **Distributed transaction across PostgreSQL and a payment provider:** generally unavailable. Real payment integration requires idempotency, an outbox/reconciliation worker, and compensating actions for provider/database disagreement.
