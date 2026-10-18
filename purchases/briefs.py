"""Buying requirements are user intent, not a merchant offer or payment authorization."""
import uuid

from django.db import transaction

from agent.safety import amounts_in, planning_amounts_in
from chat.models import ChatMessage, PhotonSession
from chat.services import redact
from core.models import Group
from purchases.models import TripPlan


@transaction.atomic
def save(member, args):
    from purchases.rules import RuleError
    session = PhotonSession.objects.select_for_update().filter(member=member).first()
    if session is None:
        raise RuleError('private_planning_only', 'Save buying requirements in the individual conversation.')
    budget = args.get('budget_per_person')
    if budget is not None:
        known = set()
        for text in ChatMessage.objects.filter(group=member.group, direction='in').order_by('-seq').values_list('text_redacted', flat=True)[:50]:
            known = planning_amounts_in(text)
            if known:
                break
        if round(budget * 100) not in known:
            raise RuleError('budget_not_confirmed', 'Use the most recent explicitly stated user budget, or omit it and ask. Never invent a budget.')
    recommendations = args.get('recommendations', [])
    if any(amounts_in(item) or 'http' in item.lower() for item in recommendations):
        raise RuleError('unverified_offer', 'General recommendations must be qualitative. Only catalog tools can supply prices or merchant offers.')
    state = {'goal':redact(args['goal']), 'category':args['category'], 'group_size':args['group_size'],
             'budget_per_person_cents':round(budget * 100) if budget is not None else None,
             'summary':redact(args.get('summary', '')), 'requirements':[redact(v) for v in args.get('requirements', [])],
             'recommendations':[redact(v) for v in recommendations]}
    previous = dict(session.planning_state)
    revision = previous.pop('revision', None)
    state['revision'] = revision if previous == state and revision else uuid.uuid4().hex
    session.planning_state = state
    session.save(update_fields=['planning_state'])
    return state


@transaction.atomic
def publish(member):
    from purchases.rules import RuleError
    from purchases.plans import plan_url
    Group.objects.select_for_update().get(pk=member.group_id)
    session = PhotonSession.objects.filter(member=member).first()
    state = session.planning_state if session else {}
    if not state:
        raise RuleError('requirements_needed', 'Save the buying goal and buyer count first.')
    plan = TripPlan.objects.filter(source_group=member.group, details__revision=state['revision']).first()
    if plan is None:
        group = Group.objects.create(name=state['goal'][:120], channel='web', timezone=member.group.timezone)
        plan = TripPlan.objects.create(source_group=member.group, group=group, title=state['goal'][:120],
            summary=state['summary'] or state['goal'], expected_members=state['group_size'], details=state)
    return {'plan_id':str(plan.id), 'title':plan.title, 'expected_members':plan.expected_members,
            'share_url':plan_url(plan), 'checkout_ready':bool(plan.selected_option_id), 'status':'quoted' if plan.selected_option_id else 'planning_only',
            'budget_per_person_cents':state.get('budget_per_person_cents')}
