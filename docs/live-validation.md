# Live validation — October 17, 2026 (PDT)

Validated against the deployed Heroku app with isolated groups named `Live check … 5ac23ef8`. Existing Tahoe demo data was not reset. Payments remained simulated.

## Passed through public HTTPS APIs

- Live Groq search and vote creation.
- Four numeric votes create one purchase and six private consent links.
- Six individual approvals lead to automatic signed booking and captures.
- Declined card 0002 can be replaced by approving card 1111.
- Leaving releases the member's authorized hold.
- Expiring an isolated unfunded purchase releases every remaining hold.
- Jordan's decline triggers the worker's fallback vote.
- Cheaper-option vote selects Juniper at $925; five reconfirmations lead to booking.
- $60 groceries split five ways produce five $12 allocations; ledger nets to zero.

## Passed signed HTTP merchant checks

A separate group tested `MERCHANT_MODE=http` within one test dyno, calling the real deployed merchant endpoints against PostgreSQL. Production mode was not changed.

- Tampered signed checkout returned digest mismatch, left the purchase funded, and captured nobody.
- A fresh signed retry booked successfully through HTTP without a transaction deadlock.

## Found: concurrent event-log sequence fork

The shared audit chain contains duplicate sequence 170. Selecting and locking only the current tail permits PostgreSQL readers waiting on the old tail to allocate the same next sequence. No event history was rewritten.

Fix: acquire a stable transaction-scoped PostgreSQL advisory lock before selecting the tail, including when the log is empty. A live PostgreSQL probe using the revised writer passed with four concurrent writers and 48 consecutive, correctly linked new events.

Historical chain integrity still fails until a separately approved repair/reset strategy is applied. Do not interpret the new lock as repairing old history. The dashboard correctly displays a failed integrity check for the historical chain.
