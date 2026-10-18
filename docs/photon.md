# Tandem iMessage demo on Photon

The `photon` process uses the official Spectrum cloud SDK and the existing Django/Celery agent. It accepts direct iMessage texts from registered users in the configured Photon project. Group messages, unauthorized senders, SMS/RCS, outbound echoes, and attachments are ignored. Every text DM invokes Tandem without requiring `@tandem`; numeric replies still use the existing ballot handler.

Required environment: `SPECTRUM_PROJECT_ID`, `SPECTRUM_PROJECT_SECRET`, `PHOTON_BRIDGE_SECRET` (32+ characters), `FIELD_ENCRYPTION_KEY` (Fernet), and HTTPS `PUBLIC_BASE_URL`. `PHOTON_TEST_PHONE` is optional: when set, the existing user's conversation and planning state continue working. Keep `CHAT_ADAPTER=web`: the SDK worker delivers the persistent chat outbox; the older Python Photon adapter is not used. Never commit credentials.

Start with `npm run photon`, or scale exactly one Heroku `photon` process. The existing Celery worker must also run. SDK cloud clients renew their line tokens automatically. Configure the Free/Pro authorized user and retrieve its assigned number before starting.

The bridge `/api/v1/chat/photon` verifies an independent HMAC over the timestamp and raw request bytes, rejects timestamps older than 60 seconds, and checks registered senders and their assigned routes. Each user gets an isolated one-member group; no seeded demo member is impersonated. Photon message IDs have durable database receipts, so repeated deliveries do not run the agent twice. Private messages are only read from that member's own group and recipient scope.

Outbound messages are acknowledged in the database after Photon accepts a send. Pending updates survive worker restarts. Delivery is at least once: a crash between sending and acknowledging can repeat the last update. Photon accepting a send does not prove handset delivery. Payments and authentication retain the app's explicitly simulated behavior.

Validation: `python manage.py test chat.tests.test_photon` and `npm run test:photon`. No public bridge request can specify a Tandem member or group identity. Do not expose the bridge secret to browser code.

## Public registration

All landing-page demo buttons open a shared phone-entry modal. Visitors connect their sending number, wait for their assigned Photon route, then open Messages and send the first text. Desktop visitors scan a QR code to continue on iPhone. Android visitors can use the browser demo. No contact is saved and registration sends no messages or invitations.

Deploy with enrollment disabled (the default), apply the migration, and start the updated Photon process. Verify the existing user still works, then set `PHOTON_PUBLIC_REGISTRATION=1`. Set it back to `0` to close new enrollment; registered users keep working. Run exactly one Photon process alongside the Celery worker. Provider capacity and iMessage availability still apply; this feature never upgrades the Photon plan or purchases dedicated lines.

- `POST /api/v1/demo/registrations` accepts `{phone}` and returns an expiring onboarding `token` and provisioning status. It is idempotent by project + normalized phone. Phones are encrypted and are never returned to the public client.
- `GET /api/v1/demo/registrations/status` accepts `X-Tandem-Onboarding`. It returns `queued`, `provisioning`, `connecting`, `ready`, `active`, or `failed`; only ready/active responses contain the assigned destination. Tickets expire after 30 minutes and authorize no chat, member, or payment access.
- QR handoffs put the ticket in a URL fragment, not a request URL or referrer. Public responses use `Cache-Control: no-store`.
- A Celery task calls Spectrum Cloud's `/projects/{id}/users/` using project ID/secret HTTP Basic authentication. It reuses one exact phone match or creates a shared user with invitations disabled. The verified response envelope is `{succeed, data}`; user records include `id`, `phoneNumber`, `assignedPhoneNumber`, and `type`.
- Readiness requires a signed connection heartbeat from the Photon process. Missing heartbeats return the public status to connecting after 60 seconds. Its authenticated registry refreshes every 15 seconds; a replacement stream is prepared before stopping the old one. Inbound overlap is deduplicated; failed outbound sends remain pending.
- Dedicated routes require the exact destination; shared routes use Spectrum's project-scoped `shared` sentinel. The signed `registry` action exposes routing data only to the Photon process.
- Phones are limited to 5 enrollment requests per 15 minutes and connecting IPs to 10. `PHOTON_REGISTRATION_DAILY_MAX` defaults to 100 provisioning attempts per rolling day. A cache outage closes enrollment rather than disabling its limits.

Run `python manage.py test chat core.test_operations`, `npm run test:photon`, `npm --prefix frontend run build`, and `npm --prefix frontend run lint`. Verify two real phones before launch, including registration, first inbound text, private plan links, duplicate enrollment, and reconnects. Monitor registration failure codes, stale heartbeats, bridge failures, and outbox backlog without logging raw phone numbers, tickets, credentials, or message contents.
