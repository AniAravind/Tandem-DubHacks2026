# Integration handoff

Implemented in this checkout; not deployed yet. No model migrations or tool/card schema changes.

## Aryan

- `send_private(group_id, member_id, message)` validates active membership and uses the adapter's private recipient.
- Real chat votes create a purchase once under a vote lock using the verified voter identity. This remains a chat-triggered handoff; Eshaan's scheduled vote closure must use the same trusted backend transition when it lands.
- Tool results are enriched for card presentation only. Purchase details and private links do not go back into the LLM context.
- Worker arguments now optionally include the triggering message ID. Old queued calls remain compatible.
- Real tools log once in the rules engine; the orchestrator records a separate tool-result event.

## Ani

- Tokenization idempotency is derived from share/version and the transient token; the browser retains that token across approval retries. No migration required.
- Mandate creation idempotency now includes the immutable consent ID to permit retrying after decline with a new card/consent.
- Consent GET accepts optional `cap_cents`; returned copy/hash matches the selected cap, and approval recomputes it.
- Additive presentation field `reconfirmation` is null or `{from_cents, to_cents, available}`. Backend-owner agreement is still needed before deployment of this field and the fresh-consent handoff.
- Reconfirmation returns 501 until `apply_share_change(share_id, consent_id)` exists. Once present, it records consent for the new amount within the previous cap and calls it. Above-cap handling intentionally requires Eshaan's fresh-limit workflow; it cannot silently raise caps.
- Consent screens refresh actual status; held members can leave. Passkeys remain explicitly simulated and are disabled outside demo mode.

## Rishab

- Signed HTTP listings, quotes, checkout and cancellation are implemented. Checkout locks the quote, requires mandate references and an idempotency key, and rejects stale/used/mismatched quotes.
- Verification requires digest coverage on checkout/cancel and uses shared cache nonce claims. A cache outage fails closed.
- Django uses hosted Redis caching automatically under Heroku's DYNO environment. Local tests retain the in-memory cache.
- Merchant-side verification writes SignedRequest audit rows. It deliberately does not append to the global EventLog during a self-HTTP request: the caller may hold the chain row lock. Rules-side accepted/rejected events retain group context. Standalone merchant attempts are visible in SignedRequest rather than the group timeline.
- Customer cancellation honors seeded non-refundable/free-until policy. Capture-failure compensation for non-refundable listings still needs an explicit agreed reversal contract between merchant and rules engine.

## Verified deployed configuration

Heroku has Postgres and Redis attachments, web.1 and worker.1 up, WEB_CONCURRENCY=3, CELERY_EAGER=0, DEBUG=0, and simulated payments. Groq smoke test returned search_options with rental/group_size=6. Merchant mode remains direct (default).

## Required from Eshaan before final rollout

1. Fallback proposal/application, versioned shares and private replacement links.
2. `apply_share_change`, with fresh consent validated against current amount, cap, merchant/listing and version.
3. Shared vote-closure transition for chat and scheduled closure; deadlines/reminders scheduled after commit.
4. Protected demo/reset tooling and above-cap fresh-consent path.
5. Confirm additive consent presentation fields and token/mandate retry semantics described above.

## Remaining rollout checks

Do not switch MERCHANT_MODE to http until a PostgreSQL self-HTTP test with real concurrent workers passes. SQLite endpoint tests cannot prove absence of cross-process lock contention. Test two final approvals concurrently, rejection before capture, signed merchant retry, worker restart, and fresh-consent fallback. Then deploy and run happy path and dropout twice after reset. These tests cannot establish fallback completion until Eshaan's implementation lands.
