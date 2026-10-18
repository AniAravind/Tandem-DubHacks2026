// Reconcile provider history when a live stream silently misses an event.
export async function recoverRoute(backend, route, client, after, seen) {
  const chat = `any;-;${route.sender}`;
  let pageToken;
  const rows = [];
  for (let page = 0; page < 5; page++) {
    const result = await client.messages.listInChat(chat, { after, isFromMe: false, pageSize: 100, pageToken });
    rows.push(...result.messages);
    pageToken = result.nextPageToken;
    if (!pageToken) break;
    if (page === 4) throw new Error('Recovery history exceeds page limit');
  }
  let count = 0;
  for (const message of rows.sort((a, b) => new Date(a.dateCreated) - new Date(b.dateCreated))) {
    const text = message.content?.text?.trim();
    const key = `${route.sender}:${message.guid}`;
    if (seen.has(key) || message.isFromMe || message.sender?.address !== route.sender
      || (message.sender?.service && message.sender.service !== 'iMessage')
      || !message.guid || !text || text.length > 2000 || new Date(message.dateCreated) < after) continue;
    // Use the same provider GUID as the live receiver. The backend receipt is
    // durable, so a restart or overlapping live event cannot enqueue it twice.
    await backend({ sender: route.sender, route: route.allocation === 'shared' ? 'shared' : route.destination,
      action: 'receive', message_id: message.guid, text });
    seen.add(key);
    if (seen.size > 10000) seen.delete(seen.values().next().value);
    count++;
  }
  return count;
}

export async function recoveryLoop(backend, stopping, credentials) {
  const { cloud } = await import('spectrum-ts');
  const { createClient } = await import('@photon-ai/advanced-imessage/grpc');
  const seen = new Set(), checked = new Map();
  let clients = [], renewedAt = 0;
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  try {
    while (!stopping()) {
      try {
        if (!clients.length || Date.now() - renewedAt > 10 * 60 * 1000) {
          const tokens = await cloud.issueImessageTokens(credentials.projectId, credentials.projectSecret);
          const next = tokens.type === 'shared'
            ? [{ phone: 'shared', client: createClient({ address: process.env.SPECTRUM_IMESSAGE_ADDRESS || 'imessage.spectrum.photon.codes:443', token: tokens.token, tls: true, timeout: 10000 }) }]
            : Object.entries(tokens.auth).map(([id, token]) => ({ phone: tokens.numbers?.[id], client: createClient({ address: `${id}.imsg.photon.codes:443`, token, tls: true, timeout: 10000 }) }));
          await Promise.allSettled(clients.map(entry => entry.client.close()));
          clients = next;
          renewedAt = Date.now();
        }
        const { routes } = await backend({ action: 'registry' });
        await Promise.all(routes.map(async route => {
          const entry = clients.find(entry => entry.phone === (route.allocation === 'shared' ? 'shared' : route.destination))
            || (route.allocation === 'legacy' && clients.length === 1 ? clients[0] : undefined);
          if (!entry) return;
          const started = Date.now();
          const after = new Date(checked.get(route.sender) || started - 60 * 60 * 1000);
          try {
            const recovered = await recoverRoute(backend, route, entry.client, after, seen);
            checked.set(route.sender, started - 30000);
            if (recovered) console.info(`Photon history reconciled ${recovered} inbound message(s).`);
          } catch { console.error('Photon history recovery unavailable; will retry.'); }
        }));
      } catch { console.error('Photon history connection unavailable; will retry.'); }
      await pause(15000);
    }
  } finally { await Promise.allSettled(clients.map(entry => entry.client.close())); }
}
