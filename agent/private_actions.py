"""Common planning actions use saved, group-scoped facts without a model call."""
import json
import re

from chat.adapters.base import AgentMessage
from chat.models import PhotonSession
from purchases.serializers import latest_options_json


def current_options(group, session):
    options = latest_options_json(group)['options']
    brief = session.planning_state
    return [o for o in options if not brief or o['meta'].get('brief_revision') == brief.get('revision')]


def current_live_offers(group, session):
    from purchases import live
    from purchases.models import LiveOffer
    brief = session.planning_state
    if '_recommendation_ids' in brief and brief.get('_recommendation_revision') == brief.get('revision'):
        rows = {str(o.id):o for o in LiveOffer.objects.filter(group=group, id__in=brief['_recommendation_ids'])}
        selected=[]
        for key in brief['_recommendation_ids']:
            if key in rows:
                rows[key].label=chr(65+len(selected))
                selected.append(rows[key])
        return selected
    return live.latest(group, brief.get('revision') if brief else None)


_COMPARE = re.compile(r'(?:please\s+)?compare\s+(?:options?\s+)?(.+)', re.I)
_LETTER = re.compile(r'(?:(?:option|pick|choose|select|go with|let[’\']s do|i want|i choose)\s+)?([A-Ea-e])', re.I)


def _compare(orchestrator, actor, text, options, offers):
    refs = re.findall(r'\b(\d|[A-Ea-e])\b', text)
    if len(refs) < 2:
        return None
    ids, labels = [], []
    for ref in refs:
        if ref.isdigit():
            match = next((o for o in options if o['rank'] == int(ref)), None)
            if match is None:
                return [AgentMessage(text=f'{ref} isn’t on your current shortlist.')]
            ids.append(match['id'])
        else:
            labels.append(ref.upper())
    result = orchestrator.run_tool('compare_options', json.dumps({'option_ids': ids, 'offer_labels': labels}), actor)
    if result.get('ok'):
        return orchestrator.compose(None)
    return [AgentMessage(text='I couldn’t compare those: ' + result['error']['message'])]


def _feature_live_offer(orchestrator, actor, label, offers):
    offer = next((o for o in offers if o.label == label), None)
    if offer is None:
        return [AgentMessage(text='That letter isn’t in the current web results. Pick ' + ', '.join(o.label for o in offers) + '.')]
    from chat.models import PhotonSession
    session=PhotonSession.objects.get(member__group=orchestrator.group)
    choice={'featured_offer_id':str(offer.id)} if session.planning_state.get('_recommendation_ids') else {'featured_offer':label}
    result = orchestrator.run_tool('publish_buying_plan', json.dumps(choice), actor)
    if result.get('ok'):
        return orchestrator.compose(None)
    return [AgentMessage(text='I couldn’t make the planning page yet: ' + result['error']['message'])]


def handle(orchestrator, actor, text):
    session = PhotonSession.objects.filter(member__group=orchestrator.group).first()
    if session is None:
        return None
    from agent.dialogue import pending
    question = pending(orchestrator.group.id)
    if re.fullmatch(r'\s*\d+(?:\.\d+)?[.!]?\s*', text) and question and question.get('answer_type') != 'selection':
        return None
    text = text.strip().rstrip('.!').strip()
    options = current_options(orchestrator.group, session)
    offers = current_live_offers(orchestrator.group, session)
    compare = _COMPARE.fullmatch(text)
    if compare and (options or offers):
        reply = _compare(orchestrator, actor, compare.group(1), options, offers)
        if reply is not None:
            return reply
    letter = _LETTER.fullmatch(text)
    if letter and offers:
        return _feature_live_offer(orchestrator, actor, letter.group(1).upper(), offers)
    match = re.fullmatch(r'(?:(?:option|number|pick|choose|select|go with|let[’\']s do|i want|i choose)\s+)?#?(\d{1,2})', text, re.I)
    ordinal = {'first':1, 'second':2, 'third':3}
    number = int(match.group(1)) if match else ordinal.get(text.lower().replace('the ', '').replace(' one', ''))
    selected = None
    if number is not None:
        selected = next((o for o in options if o['rank'] == number), None)
        if selected is None:
            if not options:
                if not re.search(r'pick|choose|option|number|select|first|second|third', text, re.I) and session.planning_state.get('_dialogue'):
                    return None
                return [AgentMessage(text='I don’t have current options to pick from yet. Tell me what you’re buying, or say “find options” for your saved plan. 🔎')]
            return [AgentMessage(text='That number isn’t on this shortlist. Pick '+', '.join(str(o['rank']) for o in options)+', or send the option’s name.')]
    else:
        title = re.sub(r'^(?:pick|choose|select|go with|i want|i choose)\s+(?:the\s+)?', '', text, flags=re.I)
        selected = next((o for o in options if title.casefold() == o['title'].casefold()), None)
    if selected:
        result = orchestrator.run_tool('publish_plan', json.dumps({'option_id':selected['id'],
            'title':selected['title'], 'summary':f"Selected {selected['title']} for {selected['group_size']} buyer(s). Review the offer together; each buyer approves their own exact payment separately."}), actor)
        if result.get('ok'):
            return orchestrator.compose(None)
        return [AgentMessage(text='I couldn’t publish that choice: '+result['error']['message']+' No payment was taken.')]
    if options and re.fullmatch(r'(?:help\s+)?(?:find|show|see)(?:\s+me)?(?:\s+(?:the|some|my))?\s+options(?:\s+please)?',text,re.I):
        orchestrator.tool_results.append(('search_options',{'options':options}))
        return orchestrator.compose(None)
    # Open-ended discovery belongs to the model: it chooses tools from the current intent.
    return None
