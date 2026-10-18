import './saved-plan.css';

export type SavedDetails = {
 decision_details?:{topic:string;value:string}[];
 plan_sections?:{heading:string;body:string}[];
 open_questions?:string[];
 featured_offer_id?:string|null;
 live_offers?:{offer_id:string;kind:string;title:string;url:string;source:string;snippet:string;price_text?:string|null;price_note?:string|null;retrieved_at?:string|null}[];
};
function sourceLink(value:string){try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password?u.href:null;}catch{return null;}}

export default function SavedPlanDetails({details}: {details:SavedDetails}) {
 const offers=details.live_offers??[];
 const kinds=[...new Set(offers.map(o=>o.kind))];
 if(!offers.length&&!details.plan_sections?.length&&!details.decision_details?.length&&!details.open_questions?.length)return null;
 return <div className="saved-plan-details">
  {!!details.decision_details?.length&&<dl className="saved-plan-facts">{details.decision_details.map((d,i)=><div key={i}><dt>{d.topic}</dt><dd>{d.value}</dd></div>)}</dl>}
  {details.plan_sections?.map((section,i)=><section className="saved-plan-section" key={i}><h3>{section.heading}</h3><p>{section.body}</p></section>)}
  {kinds.map(kind=><section className="saved-plan-section" key={kind}><h3>{({flight:'Flights',stay:'Places to stay',activity:'Things to do',gift:'Gift ideas',product:'Products',other:'Useful links'} as Record<string,string>)[kind]??kind}</h3><div className="saved-plan-links">{offers.filter(o=>o.kind===kind).map(offer=>{const url=sourceLink(offer.url);return <article key={offer.offer_id} className={offer.offer_id===details.featured_offer_id?'saved-offer selected':'saved-offer'}><span className="saved-offer-source">{offer.source}{offer.offer_id===details.featured_offer_id?' · Chosen option':''}</span><h4>{url?<a href={url} target="_blank" rel="noopener noreferrer">{offer.title}<span aria-hidden="true"> ↗</span></a>:offer.title}</h4>{offer.snippet&&<p>{offer.snippet}</p>}{offer.price_text&&<span className="saved-offer-price">{offer.price_text} · as listed, verify on source</span>}{offer.price_note&&<p className="saved-plan-disclaimer">{offer.price_note}</p>}</article>;})}</div></section>)}
  {!!offers.length&&<p className="saved-plan-disclaimer">Links found during planning. Prices and availability can change; these are not bookings or payable quotes.</p>}
  {!!details.open_questions?.length&&<section className="saved-plan-section"><h3>Still to decide</h3><ul>{details.open_questions.map((q,i)=><li key={i}>{q}</li>)}</ul><p className="saved-plan-disclaimer">Continue with your Tandem messaging agent to refine the plan.</p></section>}
 </div>;
}
