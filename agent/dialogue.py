"""Persist question/answer context independently of provider availability.

This binds replies to actual delivered questions; it does not script a buying flow.
Private dialogue metadata must never be copied to a public plan.
"""
import re
import uuid

from django.db import transaction

from chat.models import ChatMessage, PhotonSession


def question_type(text):
    if re.search(r'how many (?:people|travelers|travellers|buyers|participants|friends)|number of (?:people|travelers|travellers|buyers)|travelers count', text, re.I):
        return 'count'
    if re.search(r'(?:what|which|how much)[^?]{0,100}(?:budget|spend)|(?:budget|spend)[^?]{0,80}(?:use|amount)', text, re.I):
        return 'budget'
    if re.search(r'pick|choose|select|which (?:one|option)|reply with (?:a|the) (?:number|letter)', text, re.I):
        return 'selection'
    return 'detail'


def pending(group):
    session = PhotonSession.objects.filter(member__group_id=group).first()
    if session and session.planning_state.get('_dialogue'):
        return session.planning_state['_dialogue']
    # Bootstrap existing sessions without replaying old assistant claims as facts.
    latest = ChatMessage.objects.filter(group_id=group, direction='out').order_by('-seq').first()
    if latest and '?' in latest.text_redacted and not latest.card:
        questions = re.findall(r'[^?\n]+\?', latest.text_redacted)
        text = ' '.join(questions[-2:])[:1000]
        return {'question':text, 'answer_type':question_type(text), 'asked_seq':latest.seq}
    return {}


@transaction.atomic
def remember_question(group, message):
    session = PhotonSession.objects.select_for_update().filter(member__group_id=group).first()
    if session is None:
        return
    if message.link:
        return
    if message.card and message.card.type in {'OptionCard','LiveOffersCard','CompareCard'}:
        question, kind = 'Choose an option from the current results.', 'selection'
    elif '?' in message.text:
        questions = re.findall(r'[^?\n]+\?', message.text)
        question = ' '.join(questions[-2:])[:1000]
        kind = question_type(question)
    else:
        return  # Provider errors do not erase the question awaiting an answer.
    from chat.services import redact
    seq = ChatMessage.objects.filter(group_id=group).order_by('-seq').values_list('seq', flat=True).first() or 0
    state = dict(session.planning_state)
    state['_dialogue'] = {'question':redact(question), 'answer_type':kind, 'asked_seq':seq}
    session.planning_state = state
    session.save(update_fields=['planning_state'])


@transaction.atomic
def remember_answer(group, text, seq):
    session = PhotonSession.objects.select_for_update().filter(member__group_id=group).first()
    if session is None:
        return
    dialogue = pending(group)
    if not dialogue or seq <= dialogue.get('asked_seq', 0):
        return
    state = dict(session.planning_state)
    state['_dialogue'] = {**dialogue, 'answer':text[:1000], 'answer_seq':seq}
    # A bare amount is a budget only when the actual pending question asked for it.
    if dialogue.get('answer_type') == 'budget' and re.fullmatch(r'\s*\$?\d+(?:,\d{3})*(?:\.\d{1,2})?\s*', text):
        cents = round(float(text.strip().lstrip('$').replace(',','')) * 100)
        if 0 <= cents <= 10000000:
            if cents != state.get('budget_per_person_cents'):
                state['revision'] = uuid.uuid4().hex
            state['budget_per_person_cents'] = cents
            state['budget_basis'] = 'per_person'
            state['budget_total_cents'] = cents * state.get('group_size', 1)
            state['_budget_answer_seq'] = seq
    if dialogue.get('answer_type') == 'count' and re.fullmatch(r'\s*\d{1,2}\s*', text):
        count = int(text)
        if 1 <= count <= 20:
            if count != state.get('group_size'):
                state['revision'] = uuid.uuid4().hex
            state['group_size'] = count
            if state.get('budget_basis') == 'total' and state.get('budget_total_cents') is not None:
                state['budget_per_person_cents'] = state['budget_total_cents'] // count
            elif state.get('budget_per_person_cents') is not None:
                state['budget_total_cents'] = state['budget_per_person_cents'] * count
            state['_buyer_count_confirmed'] = True
            state['_count_answer_seq'] = seq
    session.planning_state = state
    session.save(update_fields=['planning_state'])


def public_brief(state):
    return {key:value for key,value in state.items() if not key.startswith('_')}
