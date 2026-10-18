"""A model-selected shortlist whose links, prices and evidence come from saved sources."""
from django.db import transaction
from chat.models import PhotonSession
from purchases import live


@transaction.atomic
def present(member, args):
    from purchases.rules import RuleError
    from agent import safety
    session=PhotonSession.objects.select_for_update().filter(member=member).first()
    if session is None:
        raise RuleError('private_planning_only','Present recommendations in the private conversation.')
    state=dict(session.planning_state)
    available={str(o.id):o for o in live.for_plan(member.group,state.get('revision'))}
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
    state['_recommendation_ids']=[row['offer_id'] for row in selected]
    state['_recommendation_revision']=state.get('revision')
    session.planning_state=state
    session.save(update_fields=['planning_state'])
    return {'offers':selected,'curated':True,'search_id':'shortlist','query':'','disclaimer':live.DISCLAIMER}
