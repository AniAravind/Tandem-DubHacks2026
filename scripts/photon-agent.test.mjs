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
