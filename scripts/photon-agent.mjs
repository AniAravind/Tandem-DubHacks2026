import { createHmac } from 'node:crypto';
import { pathToFileURL } from 'node:url';

import { recoveryLoop } from './photon-recovery.mjs';
import { plainText } from "../shared/message-text.mjs";

export function renderMessage(message) {
  const lines = [plainText(message.text || '')];
  const card = message.card;
  const d = card?.data || {};
  const money = value => Number.isInteger(value) ? `$${(value / 100).toFixed(2)}` : '';
  if (d.title) lines.push(card?.type === 'OptionCard' && Number.isInteger(d.rank) ? `${d.rank}. ${plainText(d.title)}` : plainText(d.title));
  if (card?.type === 'OptionCard') lines.push(`${money(d.per_person_cents)} each · ${money(d.total_cents)} total`, ...(d.features || []).slice(0, 3).map(f => f.replaceAll('_', ' ')), ...(d.why || []));
  if (d.choices || d.options) {
    for (const choice of d.choices || d.options) lines.push(`${choice.key}. ${choice.label}${Number.isInteger(choice.per_person_cents) ? ` · ${money(choice.per_person_cents)} each` : ''}`);
    lines.push(d.winner_key ? `Winner: ${d.winner_key}` : 'Reply with the option number to vote.');
  }
  if (card?.type === 'FundingCard') lines.push(`${money(d.held_cents)} held of ${money(d.total_cents)}`, ...(d.members || []).map(m => `${m.display_name}: ${m.status}`));
  if (card?.type === 'ExpenseCard') lines.push(`${d.description}: ${money(d.amount_cents)} paid by ${d.payer}`, `${d.split_count} shares · ${money(d.per_person_cents)} each`);
  if (d.booking_ref) lines.push(`Booking reference: ${d.booking_ref}`);
  if (d.dashboard_url) lines.push(d.dashboard_url);
  if (card && !lines.some(Boolean)) lines.push('Your trip has an update. Ask Tandem for its status.');
  if (d.simulated) lines.push('(Simulated payments)');
  return plainText(lines.filter(Boolean).join('\n'));
}

export function acceptsInbound(route, space, message) {
  return message.direction === 'inbound' && message.content?.type === 'text'
    && message.sender?.id === route.sender && space.type === 'dm'
    && (!message.sender?.service || message.sender.service === 'iMessage')
    && (route.allocation === 'legacy' || space.phone === (route.allocation === 'shared' ? 'shared' : route.destination));
}

export function fingerprint(routes) {
  return JSON.stringify(routes.map(r => [r.sender, r.destination, r.allocation]).sort((a, b) => a[0].localeCompare(b[0])));
}

export async function deliverPending(backend, route, dm) {
  const context = { sender: route.sender, route: route.allocation === 'shared' ? 'shared' : route.destination };
  const { messages } = await backend({ ...context, action: 'poll' });
  for (const message of messages) {
    const text = renderMessage(message);
    if (text) await dm.send(text);
    await backend({ ...context, action: 'ack', seq: message.seq });
  }
}

export async function main() {
  try { process.loadEnvFile('.env'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  for (const name of ['SPECTRUM_PROJECT_ID', 'SPECTRUM_PROJECT_SECRET', 'PHOTON_BRIDGE_SECRET', 'PUBLIC_BASE_URL']) {
    if (!process.env[name]) throw new Error(`Missing required configuration: ${name}`);
  }
  const { Spectrum } = await import('spectrum-ts');
  const { imessage } = await import('spectrum-ts/providers/imessage');
  const endpoint = new URL('/api/v1/chat/photon', process.env.PUBLIC_BASE_URL);
  if (endpoint.protocol !== 'https:' && endpoint.hostname !== 'localhost') throw new Error('Photon bridge requires HTTPS');
  async function backend(data) {
    const body = JSON.stringify(data);
    const stamp = String(Math.floor(Date.now() / 1000));
    const signature = createHmac('sha256', process.env.PHOTON_BRIDGE_SECRET).update(`${stamp}.${body}`).digest('hex');
    const response = await fetch(endpoint, { method: 'POST', body, redirect: 'error', signal: AbortSignal.timeout(25000), headers: {
      'Content-Type': 'application/json', 'X-Photon-Timestamp': stamp, 'X-Photon-Signature': signature,
    } });
    if (!response.ok) throw new Error(`Tandem bridge returned HTTP ${response.status}`);
    return response.json();
  }
  let stopping = false;
  let current;
  const recovery = recoveryLoop(backend, () => stopping, { projectId: process.env.SPECTRUM_PROJECT_ID, projectSecret: process.env.SPECTRUM_PROJECT_SECRET });
  for (const signal of ['SIGTERM', 'SIGINT']) process.once(signal, () => { stopping = true; });
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const context = route => ({ sender: route.sender, route: route.allocation === 'shared' ? 'shared' : route.destination });
  async function connect(routes) {
    const app = await Spectrum({ projectId: process.env.SPECTRUM_PROJECT_ID, projectSecret: process.env.SPECTRUM_PROJECT_SECRET,
      providers: [imessage.config()], options: { logLevel: 'warn' } });
    const runtime = { app, routes, spaces: new Map(), healthy: true, stopped: false };
    try {
      for (const route of routes) {
        const params = route.allocation === 'dedicated' ? { phone: route.destination } : undefined;
        runtime.spaces.set(route.sender, await imessage(app).space.create(route.sender, params));
      }
    } catch (error) { await app.stop(); throw error; }
    runtime.receiving = (async () => {
      try {
        for await (const [space, message] of app.messages) {
          if (runtime.stopped || stopping) break;
          const route = runtime.routes.find(r => acceptsInbound(r, space, message));
          if (!route) continue;
          const text = message.content.text?.trim();
          if (!text || text.length > 2000 || !message.id) continue;
          let accepted = false;
          for (let attempt = 0; attempt < 3 && !accepted; attempt++) {
            try { await backend({ ...context(route), action: 'receive', message_id: message.id, text }); accepted = true; }
            catch { console.error('Inbound delivery failed; retrying.'); await pause(2000); }
          }
          if (!accepted) throw new Error('Inbound delivery exhausted retries');
        }
      } catch { console.error('Photon stream interrupted; reconnecting.'); }
      finally { runtime.healthy = false; }
    })();
    return runtime;
  }
  let refreshedAt = 0;
  let heartbeatAt = 0;
  try {
    while (!stopping) {
      if (!current?.healthy || Date.now() - refreshedAt > 15000) {
        try {
          const { routes } = await backend({ action: 'registry' });
          if (!Array.isArray(routes)) throw new Error('Invalid registry');
          if (!current?.healthy || fingerprint(routes) !== fingerprint(current.routes)) {
            // Prepare a replacement before closing the working stream. Overlapping
            // receives are deduplicated by durable provider-message receipts.
            const replacement = routes.length ? await connect(routes) : undefined;
            const previous = current;
            current = replacement;
            if (previous) { previous.stopped = true; await previous.app.stop(); }
            heartbeatAt = 0;
          }
          refreshedAt = Date.now();
        } catch { console.error('Photon connection refresh unavailable; keeping working routes.'); }
      }
      if (current?.healthy) {
        if (Date.now() - heartbeatAt > 15000) {
          await Promise.allSettled(current.routes.map(route => backend({ ...context(route), action: 'connected' })));
          heartbeatAt = Date.now();
        }
        // One failed recipient must not prevent another recipient receiving replies.
        for (let index = 0; index < current.routes.length; index += 5) {
          await Promise.all(current.routes.slice(index, index + 5).map(async route => {
            try { await deliverPending(backend, route, current.spaces.get(route.sender)); }
            catch { console.error('Outbound delivery unavailable; retaining pending messages for retry.'); }
          }));
        }
      }
      await pause(2000);
    }
  } finally {
    stopping = true;
    await recovery;
    if (current) {
      current.stopped = true;
      await current.app.stop();
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => { console.error('Photon agent stopped: check backend availability and project credentials.'); process.exit(1); });
}
