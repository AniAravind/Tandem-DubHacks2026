"""Buying requirements are user intent, not a merchant offer or payment authorization."""
import uuid
import re

from django.db import transaction

from agent.safety import amounts_in, planning_amounts_in
from chat.models import ChatMessage, PhotonSession
from chat.services import redact
from core.models import Group
from purchases.models import TripPlan


def explicit_buyer_count(text):
    """Only unambiguous stated counts; leave other language to clarification."""
    total = re.search(r'\b(\d+)\s+(?:people|persons|buyers|participants)(?:\s+(?:in\s+)?total)?\b|\b(?:total(?:\s+of)?|group\s+of)\s+(\d+)\b', text, re.I)
    if total:
        return int(next(v for v in total.groups() if v))
    friends = re.search(r'\b(?:me|myself|I)\s+and\s+(\d+)\s+(?:friends|others)\b', text, re.I)
    if friends:
        return int(friends.group(1)) + 1
    buyers = re.search(r'\b(\d+)\s+buyers\b', text, re.I)
    if buyers:
        return int(buyers.group(1))
    if re.search(r'\b(?:for myself|just me|one buyer|solo purchase)\b', text, re.I):
        return 1
    return None


@transaction.atomic
def save(member, args):
    from purchases.rules import RuleError
    session = PhotonSession.objects.select_for_update().filter(member=member).first()
    if session is None:
        raise RuleError('private_planning_only', 'Save buying requirements in the individual conversation.')
    from purchases.consensus import canonical_category
    from agent.context import TRAVEL_CATEGORIES
    from agent.dialogue import public_brief
    message = ChatMessage.objects.filter(group=member.group, direction='in').order_by('-seq').first()
    latest = message.text_redacted if message else ''
    previous = dict(session.planning_state)
    category = canonical_category(args['category'])
    old_category = previous.get('category')
    travel = category in TRAVEL_CATEGORIES and old_category in TRAVEL_CATEGORIES
    changing = bool(previous.get('goal')) and (args.get('new_goal') is True or (category != old_category and not travel))
    short_reply = bool(re.fullmatch(r'\s*(?:yes|no|confirmed|correct|okay|ok|sure|\d+(?:\.\d+)?)\s*[.!]?\s*', latest, re.I))
    if changing and short_reply:
        raise RuleError('goal_change_not_confirmed', 'This is an answer to the pending question, not a new purchase. Preserve the current goal and confirmed facts.')
    base = {} if changing else previous
    count = explicit_buyer_count(latest)
    if count is not None and args['group_size'] != count:
        raise RuleError('buyer_count_mismatch', f'The user explicitly specified {count} total buyers. Use group_size={count}.')
    if base.get('_buyer_count_confirmed', bool(base.get('group_size'))) and base.get('group_size') and args['group_size'] != base['group_size'] and count is None:
        raise RuleError('buyer_count_not_confirmed', f"Keep the confirmed {base['group_size']} buyers; a short answer or reworded goal cannot change the roster.")
    budget = args.get('budget_per_person')
    if budget is not None:
        known = set()
        if not changing:
            for text in ChatMessage.objects.filter(group=member.group, direction='in').order_by('-seq').values_list('text_redacted', flat=True)[:50]:
                known = planning_amounts_in(text)
                if known:
                    break
            if not known and base.get('budget_per_person_cents') is not None:
                known.add(base['budget_per_person_cents'])
            if message and base.get('_budget_answer_seq') == message.seq:
                known = {base['budget_per_person_cents']}
        else:
            known = planning_amounts_in(latest)
        if round(budget * 100) not in known:
            raise RuleError('budget_not_confirmed', 'Use the confirmed budget from the saved brief or the current explicit budget answer. Do not repeatedly ask to confirm a budget already provided.')
    recommendations = args.get('recommendations', base.get('recommendations', []))
    if any(amounts_in(item) or 'http' in item.lower() for item in recommendations):
        raise RuleError('unverified_offer', 'General recommendations must be qualitative. Only catalog tools can supply prices or merchant offers.')
    sections = args.get('plan_sections', base.get('plan_sections', []))
    if any('http' in v.lower() for section in sections for v in section.values()):
        raise RuleError('unverified_offer', 'Keep plan sections as plain text. Source links are attached from real search results by the server.')
    details = {item['topic'].casefold():item for item in base.get('decision_details', [])}
    for item in args.get('decision_details', []):
        details[item['topic'].casefold()] = {key:redact(value) for key,value in item.items()}
    state = {**base, 'goal':base.get('goal') or redact(args['goal']), 'category':base.get('category') or category,
             'group_size':count or (base.get('group_size') if base.get('_buyer_count_confirmed', True) else None) or args['group_size'],
             '_buyer_count_confirmed':count is not None or base.get('_buyer_count_confirmed', False),
             'budget_per_person_cents':round(budget * 100) if budget is not None else base.get('budget_per_person_cents'),
             'summary':redact(args.get('summary', base.get('summary', ''))),
             'requirements':[redact(v) for v in args.get('requirements', base.get('requirements', []))],
             'recommendations':[redact(v) for v in recommendations], 'decision_details':list(details.values()),
             'open_questions':[redact(v) for v in args.get('open_questions', base.get('open_questions', []))],
             'research_kinds':list(dict.fromkeys(args.get('research_kinds', base.get('research_kinds', [])))),
             'plan_sections':[{key:redact(value) for key,value in item.items()} for item in sections]}
    revision = base.get('revision')
    constraints = ('category', 'group_size', 'budget_per_person_cents', 'requirements', 'decision_details')
    unchanged = all(base.get(key) == state.get(key) for key in constraints)
    state['revision'] = revision if unchanged and revision else uuid.uuid4().hex
    session.planning_state = state
    session.save(update_fields=['planning_state'])
    return public_brief(state)


@transaction.atomic
def publish(member, featured_offer=None, featured_offer_id=None):
    from purchases.rules import RuleError
    from purchases.plans import plan_url
    Group.objects.select_for_update().get(pk=member.group_id)
    session = PhotonSession.objects.filter(member=member).first()
    from agent.dialogue import public_brief
    state = public_brief(session.planning_state) if session else {}
    if not state.get('goal'):
        raise RuleError('requirements_needed', 'Save the buying goal and buyer count first.')
    from purchases import live
    offers = live.for_plan(member.group, state['revision'])
    current = live.latest(member.group, state['revision'])
    if featured_offer and featured_offer not in {o.label for o in current}:
        raise RuleError('not_found', 'That web result is not in the current search for this goal.')
    chosen = next((o for o in offers if str(o.id) == featured_offer_id), None) if featured_offer_id else next((o for o in current if o.label == featured_offer), None)
    if featured_offer_id and chosen is None:
        raise RuleError('not_found', 'Choose an offer ID from the current buying brief research results.')
    details = {**state, 'live_offers': [live.offer_json(o) for o in offers],
               'featured_offer': featured_offer or None, 'featured_offer_id':str(chosen.id) if chosen else None, 'live_disclaimer': live.DISCLAIMER if offers else None}
    plan = TripPlan.objects.filter(source_group=member.group, details__revision=state['revision']).first()
    if plan is None:
        group = Group.objects.create(name=state['goal'][:120], channel='web', timezone=member.group.timezone)
        plan = TripPlan.objects.create(source_group=member.group, group=group, title=state['goal'][:120],
            summary=state['summary'] or state['goal'], expected_members=state['group_size'], details=details)
    elif not plan.selected_option_id:
        # Republishing keeps the same link but shows the newest web results and the latest featured choice.
        plan.details = {**details, 'featured_offer': featured_offer or plan.details.get('featured_offer'),
                        'featured_offer_id':str(chosen.id) if chosen else plan.details.get('featured_offer_id')}
        plan.save(update_fields=['details', 'updated_at'])
    return {'plan_id':str(plan.id), 'title':plan.title, 'expected_members':plan.expected_members,
            'share_url':plan_url(plan), 'checkout_ready':bool(plan.selected_option_id), 'status':'quoted' if plan.selected_option_id else 'planning_only',
            'budget_per_person_cents':state.get('budget_per_person_cents'), 'featured_offer':plan.details.get('featured_offer'),
            'live_offer_count':len(plan.details.get('live_offers') or [])}
