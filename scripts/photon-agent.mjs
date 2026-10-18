import { createHmac } from 'node:crypto';
import { pathToFileURL } from 'node:url';

export function renderMessage(message) {
  const lines = [message.text || ''];
  const card = message.card;
  const d = card?.data || {};
  const money = value => Number.isInteger(value) ? `$${(value / 100).toFixed(2)}` : '';
  if (d.title) lines.push(d.title);
  if (card?.type === 'OptionCard') lines.push(`${money(d.per_person_cents)} each · ${money(d.total_cents)} total`, ...(d.why || []));
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
  return lines.filter(Boolean).join('\n');
}

export async function main() {
  try { process.loadEnvFile('.env'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  for (const name of ['SPECTRUM_PROJECT_ID', 'SPECTRUM_PROJECT_SECRET', 'PHOTON_BRIDGE_SECRET', 'PHOTON_TEST_PHONE', 'PUBLIC_BASE_URL']) {
    if (!process.env[name]) throw new Error(`Missing required configuration: ${name}`);
  }
  const { Spectrum } = await import('spectrum-ts');
  const { imessage } = await import('spectrum-ts/providers/imessage');
  const phone = process.env.PHOTON_TEST_PHONE;
  const endpoint = new URL('/api/v1/chat/photon', process.env.PUBLIC_BASE_URL);
  if (endpoint.protocol !== 'https:' && endpoint.hostname !== 'localhost') throw new Error('Photon bridge requires HTTPS');
  async function backend(data) {
    const body = JSON.stringify({ ...data, sender: phone });
    const stamp = String(Math.floor(Date.now() / 1000));
    const signature = createHmac('sha256', process.env.PHOTON_BRIDGE_SECRET).update(`${stamp}.${body}`).digest('hex');
    const response = await fetch(endpoint, { method: 'POST', body, redirect: 'error', signal: AbortSignal.timeout(25000), headers: {
      'Content-Type': 'application/json', 'X-Photon-Timestamp': stamp, 'X-Photon-Signature': signature,
    } });
    if (!response.ok) throw new Error(`Tandem bridge returned HTTP ${response.status}`);
    return response.json();
  }
  // Verify backend authentication before opening the message stream.
  await backend({ action: 'poll' });
  const app = await Spectrum({ projectId: process.env.SPECTRUM_PROJECT_ID, projectSecret: process.env.SPECTRUM_PROJECT_SECRET,
    providers: [imessage.config()], options: { logLevel: 'warn' } });
  const dm = await imessage(app).space.create(phone);
  let stopping = false;
  for (const signal of ['SIGTERM', 'SIGINT']) process.once(signal, async () => { stopping = true; await app.stop(); });
  console.log('Tandem single-agent iMessage connection initialized; waiting for authorized direct messages.');
  const receive = async () => {
    for await (const [space, message] of app.messages) {
      if (stopping) break;
      if (message.direction !== 'inbound' || message.content?.type !== 'text' || message.sender?.id !== phone || space.type !== 'dm') continue;
      const text = message.content.text?.trim();
      if (!text || text.length > 2000 || !message.id) continue;
      // Retrying the same provider ID is safe: the backend persists receipts.
      let accepted = false;
      for (let attempt = 0; attempt < 3 && !accepted; attempt++) {
        try { await backend({ action: 'receive', message_id: message.id, text }); accepted = true; console.log('Inbound direct message accepted by Tandem.'); }
        catch { console.error('Inbound delivery failed; retrying.'); await new Promise(resolve => setTimeout(resolve, 2000)); }
      }
      if (!accepted) throw new Error('Inbound delivery exhausted retries');
    }
  };
  const deliver = async () => {
    while (!stopping) {
      try {
        const { messages } = await backend({ action: 'poll' });
        for (const message of messages) {
          const text = renderMessage(message);
          if (text) await dm.send(text);
          await backend({ action: 'ack', seq: message.seq });
          console.log('Outbound update accepted by Photon and acknowledged.');
        }
      } catch { console.error('Outbound delivery unavailable; retaining pending messages for retry.'); }
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
  };
  try { await Promise.all([receive(), deliver()]); } finally { stopping = true; await app.stop(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => { console.error('Photon agent stopped: check backend availability, project credentials, and authorized user configuration.'); process.exit(1); });
}
