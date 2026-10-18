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


def handle(orchestrator, actor, text):
    session = PhotonSession.objects.filter(member__group=orchestrator.group).first()
    if session is None:
        return None
    text = text.strip().rstrip('.!').strip()
    options = current_options(orchestrator.group, session)
    match = re.fullmatch(r'(?:(?:option|number|pick|choose|select|go with|let[’\']s do|i want|i choose)\s+)?#?(\d{1,2})', text, re.I)
    ordinal = {'first':1, 'second':2, 'third':3}
    number = int(match.group(1)) if match else ordinal.get(text.lower().replace('the ', '').replace(' one', ''))
    selected = None
    if number is not None:
        selected = next((o for o in options if o['rank'] == number), None)
        if selected is None:
            if not options:
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
    if re.fullmatch(r'(?:help\s+)?(?:find|show|search|see|refresh)(?:\s+me)?(?:\s+(?:the|some|my))?\s+options(?:\s+please)?',text,re.I) and session.planning_state:
        if options:
            orchestrator.tool_results.append(('search_options',{'options':options}))
            return orchestrator.compose(None)
        brief=session.planning_state
        args={'category':brief['category'],'group_size':brief['group_size']}
        if brief.get('budget_per_person_cents') is not None:
            args['budget_per_person_max']=brief['budget_per_person_cents']/100
        result=orchestrator.run_tool('search_options',json.dumps(args),actor)
        if result.get('ok'):
            return orchestrator.compose(None)
        return [AgentMessage(text='I couldn’t refresh the shortlist: '+result['error']['message'])]
    return None
