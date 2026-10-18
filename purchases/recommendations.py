"""A model-selected shortlist whose links, prices and evidence come from saved sources."""
from django.db import transaction
from chat.models import PhotonSession
from purchases import live
from purchases.models import LiveOffer
from agent.source_quality import classify


@transaction.atomic
def present(member, args):
    from purchases.rules import RuleError
    from agent import safety
    session=PhotonSession.objects.select_for_update().filter(member=member).first()
    if session is None:
        raise RuleError('private_planning_only','Present recommendations in the private conversation.')
    state=dict(session.planning_state)
    available={str(o.id):o for o in LiveOffer.objects.filter(group=member.group,brief_revision=state.get('revision') or '').order_by('-created_at')[:150]}
    selected, seen=[],set()
    for choice in args['selections']:
        offer=available.get(choice['offer_id'])
        if offer is None or str(offer.id) in seen:
            raise RuleError('unknown_offer','Choose distinct offer IDs from the current brief research results.')
        row=live.offer_json(offer)
        index=choice['evidence_index']
        if index >= len(row['evidence_choices']):
            raise RuleError('unsupported_evidence','Select an index that exists in this offer’s evidence_choices.')
        evidence=row['evidence_choices'][index]
        row.update(label=chr(65+len(selected)),evidence=evidence,
            source_amounts=[{'amount_cents':cents} for cents in safety.amounts_in(offer.title+' '+evidence)])
        selected.append(row)
        seen.add(str(offer.id))
    verdicts=classify(selected) if selected else {}
    if verdicts is None:
        raise RuleError('source_verification_unavailable','Source checking is temporarily unavailable. Do not describe unverified search hits as recommendations.')
    rejected=[row['offer_id']+': '+verdicts[row['offer_id']]['reason'] for row in selected if not verdicts[row['offer_id']]['actionable']]
    if rejected and len(rejected)==len(selected):
        raise RuleError('not_actionable_source','These pages are articles or directories rather than actual options. Refine the search for direct product/property/provider pages, or select only actual options: '+'; '.join(rejected))
    selected=[row for row in selected if verdicts[row['offer_id']]['actionable']]
    for index,row in enumerate(selected):
        row['label']=chr(65+index)
    canonical=lambda kind:'product' if kind=='gift' else kind
    requested={canonical(kind) for kind in state.get('research_kinds',[])}
    supplied={canonical(row['kind']) for row in selected}
    missing=sorted(requested-supplied)
    state['research_status']={'requested':sorted(requested),'covered':sorted(supplied),'missing':missing}
    state['_recommendation_ids']=[row['offer_id'] for row in selected]
    state['_recommendation_revision']=state.get('revision')
    session.planning_state=state
    session.save(update_fields=['planning_state'])
    return {'offers':selected,'missing_components':missing,'curated':True,'search_id':'shortlist','query':'','disclaimer':live.DISCLAIMER}
