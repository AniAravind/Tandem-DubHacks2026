import { useEffect, useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import AppShell, { Icon } from "../components/layout/AppShell";
import { api, getDemoMember, setDemoMember, usdExact } from "../api/client";
import { DEMO_GROUP, resolveGroupId } from "../chat/demo";
import type { Group } from "../chat/types";
import { SHARE_STATUS } from "../components/cards/status";
import type { ShareStatus } from "../types/contracts";
import "./dashboard.css";
type SharedPlan = {id:string};

type Purchase = {id:string;state:string;option:{title:string;merchant_slug:string};total_cents:number;held_cents:number;deadline_at:string;shares:{id:string;member:{id:string;display_name:string};amount_cents:number;status:ShareStatus}[];counts:{held:number;active:number}};
type Event = {seq:number;at:string;summary:string;type:string;simulated:boolean;payload:Record<string,unknown>};
type Ledger = {expenses:{id:string;description:string;payer:string;amount_cents:number;split_count:number}[];balances:{member_id:string;display_name:string;net_cents:number}[];net_total_cents:number};
type Data = {plan:SharedPlan|null;group:Group;purchase:Purchase|null;events:Event[];chain:boolean;ledger:Ledger};

export default function DashboardPage(){
 const gid=resolveGroupId(useParams().groupId);
 const [data,setData]=useState<Data|null>(null),[error,setError]=useState(""),[raw,setRaw]=useState(false),[retry,setRetry]=useState(0),[now,setNow]=useState(()=>Date.now());
 useEffect(()=>{let active=true;let busy=false;let seq=0;let events:Event[]=[];
  if(gid===DEMO_GROUP.id&&!getDemoMember())setDemoMember(DEMO_GROUP.members[0].id);
  async function load(){if(busy)return;busy=true;try{
   const group=await api.get<Group & {current_purchase_id:string|null;plan:SharedPlan|null}>(`/groups/${gid}?include=workspace`);
   if(group.plan){if(active){setData({plan:group.plan,group,purchase:null,events:[],chain:true,ledger:{expenses:[],balances:[],net_total_cents:0}});setError("");}return;}
   const [eventPage,ledger]=await Promise.all([api.get<{events:Event[];chain_ok:boolean}>(`/groups/${gid}/events?after_seq=${seq}`),api.get<Ledger>(`/groups/${gid}/ledger`)]);
   if(!active)return;
   const purchase=group.current_purchase_id?await api.get<Purchase>(`/purchases/${group.current_purchase_id}`):null;
   events=[...events,...eventPage.events.filter(e=>!events.some(old=>old.seq===e.seq))];if(eventPage.events.length)seq=eventPage.events.at(-1)!.seq;
   if(active){setData({plan:group.plan,group,purchase,events,chain:eventPage.chain_ok,ledger});setError("");}
  }catch(e){if(active)setError(e instanceof Error?e.message:"Couldn’t connect to your group.");}finally{busy=false;}}
  load();const interval=setInterval(load,2000);return()=>{active=false;clearInterval(interval);};
 },[gid,retry]);
 useEffect(()=>{const t=setInterval(()=>setNow(Date.now()),30000);return()=>clearInterval(t);},[]);
 const p=data?.purchase;const activeShares=p?.shares.filter(s=>!["declined","released","refunded"].includes(s.status))??[];const held=activeShares.filter(s=>["held","captured"].includes(s.status)).length;const percent=p?Math.min(100,Math.round(p.held_cents/Math.max(1,p.total_cents)*100)):0;
 const left=p?Math.max(0,new Date(p.deadline_at).getTime()-now):0;const remaining=left>=3600000?`${Math.floor(left/3600000)}h ${Math.floor(left/60000)%60}m`:`${Math.floor(left/60000)}m`;
 if(data?.plan)return <Navigate to={`/p/${data.plan.id}`} replace/>;
 return <AppShell groupId={gid} planName={data?.group.name??"Your buying plan"}>
 <main className="dashboard"><header className="dashboard-heading"><div><h1>{data?.group.name??"Your group"}</h1>{data&&<p className="dashboard-subtitle">{data.group.members.length} people<span className="mode-label">{data.group.payments_mode==="visa_sandbox"?"Visa sandbox · test mode":"Simulated payments"}</span></p>}</div><Link className="btn" to={`/chat/${gid}`}>Open chat<Icon name="arrow"/></Link></header>
 {error&&<div className="banner" role="alert">{data?"Updates paused. ":""}{error} <button className="text-button" onClick={()=>setRetry(r=>r+1)}>Try again</button></div>}
 {!data&&!error?<div className="dashboard-skeleton" aria-busy="true" aria-label="Loading group overview"><div/><div/><div/></div>:!data?<section className="dash-panel empty-panel"><Icon name="chat"/><h2>We couldn’t load this group.</h2><Link className="btn primary" to={`/chat/${gid}`}>Open group chat</Link></section>:<>
 {p&&<div className="trip-overview"><div><h2>{p.option.title}</h2></div><span className="status-pill">{p?({collecting:"Gathering the group",funded:"Everyone’s in",booked:"Booked & ready",released:"Holds released",refunded:"Refunded"}[p.state]??p.state):"Making plans"}</span></div>}
 <div className="dashboard-grid">{p?<><section className="dash-panel funding-panel"><div className="section-heading"><h2>Funding</h2><span className="subtle-label">Nobody’s charged until everyone’s in</span></div><div className="funding-body"><div className="funding-ring" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-label={p?`${held} of ${activeShares.length} shares secured`:"No purchase created yet"}><svg viewBox="0 0 180 180" aria-hidden="true"><circle cx="90" cy="90" r="77" className="ring-track"/><circle cx="90" cy="90" r="77" className="ring-fill" strokeDasharray={`${percent*4.838} 483.8`}/></svg><div><strong>{p?`${held}/${activeShares.length}`:"—"}</strong><span>{p?"shares secured":"ready when you are"}</span></div></div><div className="funding-detail"><strong className="funding-amount money">{usdExact(p.held_cents)}</strong><p>held of {usdExact(p.total_cents)}</p></div></div></section>
 <section className="dash-panel deadline-panel"><span className="eyebrow">Deadline</span><Icon name="clock"/><h2>{left?remaining:"Deadline reached"}</h2><p>{new Date(p.deadline_at).toLocaleString("en-US",{weekday:"short",month:"short",day:"numeric",hour:"numeric",minute:"2-digit",timeZone:data.group.timezone})}</p></section></>:<section className="dash-panel quiet-start"><p>Pick a place in the chat. Everyone holds only their own share.</p><Link className="btn primary" to={`/chat/${gid}`}>Start in chat<Icon name="arrow"/></Link></section>}
 <section className="dash-panel members-panel"><div className="section-heading"><h2>People</h2></div><div className="member-grid">{data.group.members.map((m,i)=>{const share=p?.shares.find(s=>s.member.id===m.id);const status=share?SHARE_STATUS[share.status]:null;return <div className="member-row" key={m.id}><span className={`member-initial member-color-${i%3}`}>{m.display_name.slice(0,1)}</span><div><strong>{m.display_name}</strong>{m.role==="organizer"&&<span>Organizer</span>}</div>{status&&<span className={`member-state ${share?.status??""}`}>{status.glyph} {status.label}</span>}</div>;})}</div></section>
 <section className="dash-panel activity-panel"><div className="section-heading"><h2>Activity</h2><button className="text-button" aria-pressed={raw} onClick={()=>setRaw(!raw)}>{raw?"Human view":"Raw JSON"}</button></div>{data.events.length?<ol className="activity-list">{data.events.slice(-8).reverse().map(e=><li className={e.type.includes("rejected")?"event-rejected":""} key={e.seq}><span className="event-point"/><div>{raw?<pre>{JSON.stringify(e.payload,null,2)}</pre>:<p>{e.summary}</p>}<time dateTime={e.at}>{new Date(e.at).toLocaleTimeString("en-US",{hour:"numeric",minute:"2-digit",timeZone:data.group.timezone})}</time></div></li>)}</ol>:<p className="quiet-empty">Nothing yet.</p>}<div className={`integrity ${data.chain?"":"bad"}`}><Icon name="shield"/>{data.chain?"Log integrity verified":"Log integrity check failed"}</div></section>
 <section className="dash-panel expenses-panel"><div className="section-heading"><h2>Shared expenses</h2></div>{data.ledger.expenses.length?<ul className="expense-list">{data.ledger.expenses.map(e=><li key={e.id}><div><strong>{e.description}</strong><span>Paid by {e.payer} · {e.split_count} ways</span></div><strong className="money">{usdExact(e.amount_cents)}</strong></li>)}</ul>:<p className="quiet-empty">Say “@tandem I paid $60 for groceries” in the chat to split one.</p>}{data.ledger.balances.some(b=>b.net_cents!==0)&&<div className="balance-list">{data.ledger.balances.filter(b=>b.net_cents!==0).map(b=><div key={b.member_id}><span>{b.display_name}</span><span>{b.net_cents<0?"Owes":"Gets back"} {usdExact(Math.abs(b.net_cents))}</span></div>)}</div>}<div className="net-total"><span>Balance</span><strong className="money">{usdExact(data.ledger.net_total_cents)} {data.ledger.net_total_cents===0?"✓":""}</strong></div></section></div></>}
 </main></AppShell>;
}
