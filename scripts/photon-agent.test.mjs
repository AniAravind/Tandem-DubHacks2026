import test from 'node:test';
import assert from 'node:assert/strict';
import { renderMessage } from './photon-agent.mjs';

test('private consent URL stays in the individual reply', () => {
  assert.equal(renderMessage({ text: 'Your consent\nhttps://example.com/c/token' }), 'Your consent\nhttps://example.com/c/token');
});
test('vote choices retain authoritative amounts and ballot numbers', () => {
  const text = renderMessage({ card: { type: 'VoteCard', data: { choices: [{ key: '1', label: 'Pinecrest', per_person_cents: 17500 }] } } });
  assert.match(text, /1\. Pinecrest · \$175\.00 each/);
  assert.match(text, /Reply with the option number/);
});
test('funding status uses actual hold amounts and simulated label', () => {
  const text = renderMessage({ card: { type: 'FundingCard', data: { title: 'Cabin', held_cents: 5000, total_cents: 10000, members: [{ display_name: 'Aryan', status: 'held' }], simulated: true } } });
  assert.match(text, /\$50\.00 held of \$100\.00/);
  assert.match(text, /Aryan: held/);
  assert.match(text, /Simulated payments/);
});
