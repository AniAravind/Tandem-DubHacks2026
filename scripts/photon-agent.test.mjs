import test from 'node:test';
import assert from 'node:assert/strict';
import { renderMessage, acceptsInbound, fingerprint, deliverPending } from './photon-agent.mjs';

test('inbound routing requires registered sender, direct iMessage and assigned route', () => {
  const route = { sender: '+12065550123', destination: '+12065550999', allocation: 'shared' };
  const space = { type: 'dm', phone: 'shared' };
  const message = { direction: 'inbound', content: { type: 'text', text: 'hello' }, sender: { id: route.sender, service: 'iMessage' } };
  assert.equal(acceptsInbound(route, space, message), true);
  assert.equal(acceptsInbound(route, space, { ...message, sender: { id: '+12065550124' } }), false);
  assert.equal(acceptsInbound(route, { ...space, type: 'group' }, message), false);
  assert.equal(acceptsInbound(route, space, { ...message, direction: 'outbound' }), false);
  assert.equal(acceptsInbound(route, space, { ...message, sender: { id: route.sender, service: 'SMS' } }), false);
  assert.equal(acceptsInbound({ ...route, allocation: 'dedicated' }, space, message), false);
  assert.equal(acceptsInbound({ ...route, allocation: 'dedicated' }, { ...space, phone: route.destination }, message), true);
});

test('route fingerprint is stable across ordering and changes when a number is added', () => {
  const a = { sender: '+1', destination: '+2', allocation: 'shared' };
  const b = { sender: '+3', destination: '+4', allocation: 'shared' };
  assert.equal(fingerprint([a, b]), fingerprint([b, a]));
  assert.notEqual(fingerprint([a]), fingerprint([a, b]));
});

test('outbound acknowledgement follows successful send and stays scoped to its sender', async () => {
  const events = [];
  const route = { sender: '+12065550123', destination: '+12065550999', allocation: 'shared' };
  const backend = async data => { events.push(data); return { messages: [{ seq: 4, text: 'Your plan' }] }; };
  const dm = { send: async text => events.push(text) };
  await deliverPending(backend, route, dm);
  assert.deepEqual(events, [{ sender: route.sender, route: 'shared', action: 'poll' }, 'Your plan',
    { sender: route.sender, route: 'shared', action: 'ack', seq: 4 }]);
  events.length = 0;
  await assert.rejects(deliverPending(backend, route, { send: async () => { throw new Error('offline'); } }));
  assert.equal(events.length, 1);
});

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

test('iMessage headings and bold become clean plain-text titles', () => {
  assert.equal(renderMessage({text:'## 🎁 A gift for Rishab\n\n**Good pick!**\nChoose `1`.'}), '🎁 A gift for Rishab\n\nGood pick!\nChoose 1.');
});
test('legacy markdown tables become numbered readable options', () => {
  const text=renderMessage({text:'| # | Title | Cost per person |\n|---|---|---|\n| 1 | **Game night** | **$5.00** |\n| 2 | Coffee | $12.00 |'});
  assert.match(text,/1\. Game night\nCost per person: \$5.00/);
  assert.match(text,/2\. Coffee/);
  assert.doesNotMatch(text,/\||\*\*/);
});
test('option cards expose the same selection number as the saved rank', () => {
  const text=renderMessage({card:{type:'OptionCard',data:{rank:2,title:'Coffee tasting gift box',per_person_cents:1200,total_cents:6000,features:['gift_wrap']}}});
  assert.match(text,/^2\. Coffee tasting gift box/);
  assert.match(text,/gift wrap/);
});
test('signed URL characters are never modified by formatting', () => {
  const url='https://example.com/p/id?owner=ab__cd__ef&x=1';
  assert.equal(renderMessage({text:`✨ Your plan\n${url}`}),`✨ Your plan\n${url}`);
});

test('live web results and comparisons render once, with their links intact', () => {
  const text = 'A. Blue Bear Cabin\n$335/night listed on vrbo.com\nhttps://www.vrbo.com/2023227312345678ha';
  const live = renderMessage({ text, card: { type: 'LiveOffersCard', data: { offers: [{ label: 'A', title: 'Blue Bear Cabin' }], live: true } } });
  assert.equal(live, text);
  const compare = renderMessage({ text: '⚖️ Side by side\n\n1. Pinecrest', card: { type: 'CompareCard', data: { rows: [{ ref: '1' }] } } });
  assert.equal(compare, '⚖️ Side by side\n\n1. Pinecrest');
});
