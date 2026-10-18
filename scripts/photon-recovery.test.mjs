import test from 'node:test';
import assert from 'node:assert/strict';
import { recoverRoute } from './photon-recovery.mjs';
const route = {sender:'+12065550123', allocation:'shared'};
const after = new Date('2026-10-18T14:00:00Z');
const message = (guid, dateCreated='2026-10-18T14:03:00Z') => ({guid,dateCreated, sender:{address:route.sender,service:'iMessage'},content:{text:'hello'},isFromMe:false});
test('missed messages recover oldest first with live receipt IDs and remain isolated', async () => {
 const events=[], seen=new Set();
 const client={messages:{listInChat:async chat=>{assert.equal(chat,`any;-;${route.sender}`);return {messages:[message('second','2026-10-18T14:04:00Z'),message('first'),{...message('other'),sender:{address:'+12065550999'}},{...message('out'),isFromMe:true},message('old','2026-10-18T13:00:00Z')]};}}};
 assert.equal(await recoverRoute(async data=>events.push(data),route,client,after,seen),2);
 assert.deepEqual(events.map(x=>x.message_id),['first','second']);
 assert.ok(events.every(x=>x.route==='shared'&&x.sender===route.sender));
 assert.equal(await recoverRoute(async data=>events.push(data),route,client,after,seen),0);
});
test('failed ingestion is retained for retry and history pages are followed', async()=>{
 const seen=new Set(),calls=[];
 const client={messages:{listInChat:async (_,options)=>{calls.push(options.pageToken);return options.pageToken?{messages:[message('first')]}:{messages:[],nextPageToken:'next'};}}};
 await assert.rejects(recoverRoute(async()=>{throw Error('offline');},route,client,after,seen));
 assert.equal(seen.size,0);
 assert.equal(await recoverRoute(async()=>{},route,client,after,seen),1);
 assert.deepEqual(calls,[undefined,'next',undefined,'next']);
});
