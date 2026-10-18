# Single Tandem agent on Photon

The `photon` process uses the official Spectrum cloud SDK and the existing Django/Celery agent. It accepts direct text messages from exactly `PHOTON_TEST_PHONE`, in the configured Photon project. Group messages, other senders, outbound echoes, and attachments are ignored. Every text DM invokes Tandem without requiring `@tandem`; numeric replies still use the existing ballot handler.

Required environment: `SPECTRUM_PROJECT_ID`, `SPECTRUM_PROJECT_SECRET`, `PHOTON_BRIDGE_SECRET`, `PHOTON_TEST_PHONE`, and HTTPS `PUBLIC_BASE_URL`. Keep `CHAT_ADAPTER=web`: the SDK worker delivers the persistent chat outbox; the older Python Photon adapter is not used. Never commit credentials.

Start with `npm run photon`, or scale exactly one Heroku `photon` process. The existing Celery worker must also run. SDK cloud clients renew their line tokens automatically. Configure the Free/Pro authorized user and retrieve its assigned number before starting.

The bridge `/api/v1/chat/photon` verifies an independent HMAC over the timestamp and raw request bytes, rejects timestamps older than 60 seconds, and checks the configured sender. The authorized user gets an isolated one-member group; no seeded demo member is impersonated. Photon message IDs have durable database receipts, so repeated deliveries do not run the agent twice. Private messages are only read from that member's own group and recipient scope.

Outbound messages are acknowledged in the database after Photon accepts a send. Pending updates survive worker restarts. Delivery is at least once: a crash between sending and acknowledging can repeat the last update. Photon accepting a send does not prove handset delivery. Payments and authentication retain the app's explicitly simulated behavior.

Validation: `python manage.py test chat.tests.test_photon` and `npm run test:photon`. No public bridge request can specify a Tandem member or group identity. Do not expose the bridge secret to browser code.
