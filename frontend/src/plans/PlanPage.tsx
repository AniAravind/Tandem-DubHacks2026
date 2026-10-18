import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { api, setDemoMember, usdExact } from '../api/client';
import { Mark, Icon } from '../components/layout/AppShell';
import './plan.css';
import SavedPlanDetails, {type SavedDetails} from './SavedPlanDetails';

type Plan = {
 id:string; title:string; summary:string; share_url:string; group_id:string; expected_members:number; joined_count:number; expired:boolean;
 checkout_ready:boolean; payment_simulation:boolean; simulation_available:boolean; category:string; details:SavedDetails & {budget_per_person_cents?:number|null;recommendations?:string[]};
 option:{title:string;total_cents:number|null;per_person_cents:number|null;currency:string;features:string[];photo_url:string|null;check_in:string|null;check_out:string|null;cancellation:string};
 me:{id:string;display_name:string;organizer:boolean}|null; members:{id:string;display_name:string}[];
 purchase:{id:string;state:string;deadline_at:string}|null;
 my_share:{amount_cents:number;status:string;consent_url:string|null}|null;
};
const shareLabels:Record<string,string> = {invited:'Ready for your approval',viewing:'Ready for your approval',held:'Your share is held',captured:'Your share is paid',card_failed:'Your card needs another try',declined:'You declined this share',released:'Your hold was released',refunded:'Your payment was refunded'};
const date = (value:string|null) => value ? new Date(value+'T12:00:00').toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}) : 'Dates to be confirmed';

export default function PlanPage(){
 const {planId}=useParams(); const [search]=useSearchParams();
 const [plan,setPlan]=useState<Plan|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[loaded,setLoaded]=useState(false),[copied,setCopied]=useState(false);
 const [name,setName]=useState(''),[phone,setPhone]=useState(''),[accept,setAccept]=useState(false);
 const [demoTotal,setDemoTotal]=useState(''),[confirmSimulation,setConfirmSimulation]=useState(false);
 const paymentSetupAttempt=useRef<string|null>(null);
 useEffect(()=>{
  if(!plan?.me?.organizer||!plan.checkout_ready||plan.purchase||plan.expired||plan.joined_count!==plan.expected_members||paymentSetupAttempt.current===plan.id)return;
  paymentSetupAttempt.current=plan.id;
  let active=true;
  api.post<Plan>(`/plans/${plan.id}/fund`).then(data=>{if(active)setPlan(data);}).catch(e=>{if(active)setError(e instanceof Error?e.message:'Could not prepare payments. Please try opening payment collection again.');});
  return()=>{active=false;};
 },[plan]);
 useEffect(()=>{let active=true;let loading=false;
  async function load(){if(loading)return;loading=true;try{const data=await api.get<Plan>(`/plans/${planId}`);if(active){setPlan(data);setLoaded(true);}}catch(e){if(active){setError(e instanceof Error?e.message:'Could not load this plan.');setLoaded(true);}}finally{loading=false;}}
  load();const timer=setInterval(load,4000);return()=>{active=false;clearInterval(timer);};
 },[planId]);
 async function join(event:FormEvent){event.preventDefault();setBusy(true);setError('');try{
  const data=await api.post<Plan>(`/plans/${planId}/join`,{name:name.trim(),phone:phone.trim(),accept_plan:accept,owner_token:search.get('owner')||undefined});
  setDemoMember(null);setPlan(data);window.history.replaceState(null,'',`/p/${planId}`);
 }catch(e){setError(e instanceof Error?e.message:'Could not join. Please try again.');}finally{setBusy(false);}}
 async function fund(simulate=false){setBusy(true);setError('');try{setPlan(await api.post<Plan>(`/plans/${planId}/fund`,simulate?{demo_total_cents:Math.round(Number(demoTotal)*100),confirm_simulation:confirmSimulation}:{}));}catch(e){setError(e instanceof Error?e.message:'Could not open payment collection.');}finally{setBusy(false);}}
 async function copy(){if(!plan)return;try{await navigator.clipboard.writeText(plan.share_url);setCopied(true);}catch{setError('Copy the group invite link shown below.');}}
 const total=plan?.checkout_ready?plan.option.total_cents:null;
 const budget=plan?.details.budget_per_person_cents;
 const share=plan?.my_share?.amount_cents??(plan?.checkout_ready?plan.option.per_person_cents:null);
 return <div className="plan-page">
 <header className="plan-nav"><Link to="/" className="plan-brand"><Mark/>tandem<span>.</span></Link><span>Better plans, together.</span></header>
 <main id="workspace" className="plan-main">
 {!loaded?<div className="plan-loading" role="status"><h1>Opening your plan...</h1><div/><div/></div>:!plan?<section className="plan-panel"><h1>This plan couldn't be opened.</h1><p role="alert">{error}</p><button className="btn" onClick={()=>window.location.reload()}>Try again</button></section>:<>
 <header className="plan-heading"><div><p className="eyebrow">YOUR SHARED PLAN</p><h1>{plan.title}</h1><p className="plan-subtitle">{plan.option.check_in?`${date(plan.option.check_in)}${plan.option.check_out?` - ${date(plan.option.check_out)}`:''}`:`${plan.expected_members} ${plan.expected_members===1?'person':'people'}`}</p></div><button className="btn primary" onClick={copy}>{copied?'Link copied':'Copy invite link'}<Icon name="arrow"/></button><span className="sr-only" role="status">{copied?'Invite link copied':''}</span></header>
 {error&&<div className="plan-error" role="alert">{error}<button aria-label="Dismiss error" onClick={()=>setError('')}>x</button></div>}
 <section className="plan-totals" aria-label="Plan totals">
 <div className="plan-total-primary"><span>{total!=null?'Current total':budget!=null?'Group budget':'Current total'}</span><strong className="money">{total!=null?usdExact(total):budget!=null?usdExact(budget*plan.expected_members):'Not set yet'}</strong><small>{total!=null?'Planned amount':budget!=null?'Budget - final price to be confirmed':'Waiting for a final price'}</small></div>
 <div><span>{share!=null?'Your share':'Per person'}</span><strong className="money">{share!=null?usdExact(share):budget!=null?usdExact(budget):'--'}</strong><small>{plan.my_share?shareLabels[plan.my_share.status]??plan.my_share.status:share!=null?'Estimated split':budget!=null?'Budget per person':'To be confirmed'}</small></div>
 <div><span>People joined</span><strong>{plan.joined_count}<em> / {plan.expected_members}</em></strong><small>{plan.joined_count===plan.expected_members?"Everyone's here":`${plan.expected_members-plan.joined_count} ${plan.expected_members-plan.joined_count===1?'place':'places'} remaining`}</small></div>
 </section>
 <div className="plan-layout"><section className="plan-panel plan-story"><div className="plan-section-title"><h2>The buying plan</h2><Icon name="grid"/></div>
 {plan.option.photo_url&&<img className="plan-photo" src={plan.option.photo_url} alt={plan.option.title}/>}
 <p className="plan-summary">{plan.summary}</p>{plan.checkout_ready&&plan.option.title!==plan.title&&<p className="plan-selected"><strong>Selected option</strong>{plan.option.title}</p>}
 {!!plan.details.recommendations?.length&&<ul className="plan-recommendations">{plan.details.recommendations.map(item=><li key={item}>{item}</li>)}</ul>}
 <details className="plan-extra"><summary>Details, itinerary &amp; source links</summary><SavedPlanDetails details={plan.details}/></details>{plan.option.cancellation&&<details className="plan-extra"><summary>Cancellation details</summary><p>{plan.option.cancellation}</p></details>}
 </section>
 <aside className="plan-side"><section className="plan-panel"><div className="plan-section-title"><h2>Who's joined</h2><span>{plan.joined_count}/{plan.expected_members}</span></div>
 <div className="plan-progress" role="progressbar" aria-label="People who have joined" aria-valuemin={0} aria-valuemax={plan.expected_members} aria-valuenow={plan.joined_count}><div style={{width:`${Math.min(100,100*plan.joined_count/plan.expected_members)}%`}}/></div>
 {plan.me?<ul className="plan-people">{plan.members.map(m=><li key={m.id}><span aria-hidden="true">{m.display_name.slice(0,1).toUpperCase()}</span><strong>{m.display_name}{m.id===plan.me?.id?' (you)':''}</strong><small>Joined</small></li>)}</ul>:<p className="fine">Join to see everyone in the plan.</p>}
 <details className="plan-extra"><summary>Invite link</summary><a className="plan-invite-url" href={plan.share_url}>{plan.share_url}</a></details></section>
 {!plan.me?<section className="plan-panel plan-join"><h2>Join the plan</h2><p>Your name and number. That's it.</p><form onSubmit={join}>
 <label htmlFor="join-name">Your name</label><input id="join-name" autoComplete="name" required maxLength={60} value={name} onChange={e=>setName(e.target.value)} placeholder="Your name"/>
 <label htmlFor="join-phone">Phone number</label><input id="join-phone" type="tel" autoComplete="tel" inputMode="tel" required maxLength={24} value={phone} onChange={e=>setPhone(e.target.value)} placeholder="(415) 555-1234" aria-describedby="phone-help"/>
 <small id="phone-help">We add +1 for US numbers. Include your country code for other countries.</small><label className="plan-check"><input type="checkbox" required checked={accept} onChange={e=>setAccept(e.target.checked)}/><span>I agree to this plan. I'll approve my payment separately.</span></label>
 <button className="btn primary" disabled={busy||plan.expired||plan.joined_count>=plan.expected_members||!!plan.purchase}>{busy?'Joining...':plan.expired?'Link expired':plan.joined_count>=plan.expected_members?'Group is full':'Join plan'}<Icon name="arrow"/></button></form><p className="fine">Your number stays private and isn't verified.</p></section>:<section className="plan-panel plan-your-share"><h2>Your next step</h2>
 {plan.my_share?<><p className={`plan-share-status status-${plan.my_share.status}`}>{shareLabels[plan.my_share.status]||plan.my_share.status}</p>{plan.my_share.consent_url?<Link className="btn primary" to={plan.my_share.consent_url}>{plan.my_share.status==='card_failed'?'Try another card':'Review your share'}<Icon name="arrow"/></Link>:<p className="fine">{plan.my_share.status==='held'?'Waiting for the remaining approvals.':plan.my_share.status==='captured'?'Everyone has approved their share.':'Your latest payment status is shown above.'}</p>}</>:<>
 <p>{plan.joined_count===plan.expected_members?"Everyone has joined. We're preparing your shares.":'Your payment step appears once everyone has joined.'}</p>
 {plan.me.organizer&&plan.checkout_ready&&<button className="btn primary" onClick={()=>fund()} disabled={busy||plan.expired||plan.joined_count!==plan.expected_members}>{busy?'Preparing...':'Prepare shares'}</button>}
 {!plan.checkout_ready&&<p className="fine">Final pricing is still to be confirmed.</p>}
 {!plan.checkout_ready&&plan.simulation_available&&plan.joined_count===plan.expected_members&&(plan.me.organizer?<details className="plan-extra"><summary>Preview the payment flow</summary><form className="plan-demo-form" onSubmit={e=>{e.preventDefault();void fund(true);}}><label htmlFor="preview-total">Preview total (USD)</label><input id="preview-total" type="number" inputMode="decimal" min={plan.expected_members/100} max={Math.min(10000,plan.expected_members*2000)} step="0.01" required value={demoTotal} onChange={e=>setDemoTotal(e.target.value)} placeholder="120.00"/><label className="plan-check"><input type="checkbox" required checked={confirmSimulation} onChange={e=>setConfirmSimulation(e.target.checked)}/><span>No real money will move and no order will be placed.</span></label><button className="btn primary" disabled={busy||!confirmSimulation||plan.expired}>{busy?'Preparing...':'Prepare preview'}</button></form></details>:<p className="fine">Your organizer is confirming the total.</p>)}
 </>}</section>}
 </aside></div><footer className="plan-footer">Keep planning with your Tandem messaging agent.</footer>
 </>}
 </main></div>;
}
