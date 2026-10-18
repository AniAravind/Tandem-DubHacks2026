"""Private planning → shared trip. Joining is a lightweight, unverified identity claim."""
import hashlib
import re
import uuid
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from core.events import log_event
from core.models import Group, Member
from core.session import member_id_from_request, set_session_cookie
from purchases.api import ApiError, api_errors, current_member
from purchases.models import Ballot, Option, Purchase, Share, TripPlan, Vote
from purchases.serializers import iso

OWNER_SALT = 'tandem.plan-owner'


def private_planner(member):
    from chat.models import PhotonSession
    return PhotonSession.objects.filter(member=member).exists()


def plan_url(plan):
    return f'{settings.PUBLIC_BASE_URL.rstrip("/")}/p/{plan.id}'


def owner_url(plan):
    token = signing.dumps({'plan': str(plan.id)}, salt=OWNER_SALT)
    return plan_url(plan) + '?owner=' + token


def expires_at(plan):
    return plan.vote.closes_at if plan.vote_id else plan.created_at + timedelta(days=7)


@transaction.atomic
def publish(member, args):
    from chat.services import redact
    from purchases.rules import RuleError
    if not private_planner(member):
        raise RuleError('private_planning_only', 'Publish a plan from your private Tandem conversation.')
    Group.objects.select_for_update().get(pk=member.group_id)
    try:
        source_id = uuid.UUID(args['option_id'])
    except (ValueError, TypeError):
        raise RuleError('not_found', 'Choose an option returned by your search.')
    source = Option.objects.filter(pk=source_id, group_id=member.group_id).first()
    if source is None:
        raise RuleError('not_found', 'Search for options first, then choose one of those option IDs.')
    size = source.meta.get('group_size')
    if not isinstance(size, int) or not 1 <= size <= 20:
        raise RuleError('invalid_group_size', 'Search using the intended buyer count before publishing.')
    from chat.models import PhotonSession
    brief = PhotonSession.objects.get(member=member).planning_state
    if brief and source.meta.get('brief_revision') != brief.get('revision'):
        raise RuleError('stale_option', 'Those options belong to an older buying goal. Search for the current goal first.')
    plan = TripPlan.objects.filter(source_group=member.group, selected_option__meta__source_option_id=str(source.id)).first()
    if plan is None:
        group = Group.objects.create(name=redact(args['title']), channel='web', timezone=member.group.timezone)
        copied = None
        search_id = source.meta.get('search_id')
        sources = Option.objects.filter(group=member.group, meta__search_id=search_id) if search_id else [source]
        for option in sources:
            # Copy public trip facts, never the private transcript or individual budget facts.
            clone = Option.objects.create(group=group, merchant_slug=option.merchant_slug, listing_ref=option.listing_ref,
                category=option.category, title=option.title, total_cents=option.total_cents, currency=option.currency,
                capacity=option.capacity, features=option.features, score=option.score,
                explanation_facts=[f for f in option.explanation_facts if f.get('kind') != 'fits_all_budgets'],
                meta={**option.meta, 'source_option_id': str(option.id)})
            if option.id == source.id:
                copied = clone
        vote = Vote.objects.create(group=group, purpose='pick_option', choices=[{'key': '1', 'option_id': str(copied.id), 'label': copied.title}],
                                   quorum=size, closes_at=timezone.now() + timedelta(days=7))
        plan = TripPlan.objects.create(source_group=member.group, group=group, selected_option=copied, vote=vote,
                                      title=redact(args['title']), summary=redact(args['summary']), expected_members=size, details=brief)
        log_event('plan.published', actor=f'member:{member.id}', group_id=group.id,
                  payload={'plan_id': str(plan.id), 'expected_members': size, 'title': plan.title})
    return {'plan_id': str(plan.id), 'title': plan.title, 'summary': plan.summary,
            'expected_members': plan.expected_members, 'share_url': plan_url(plan)}


def payload(plan, request):
    member = Member.objects.filter(pk=member_id_from_request(request), group=plan.group, status='active').first()
    purchase = Purchase.objects.filter(group=plan.group).order_by('-created_at').first()
    option = purchase.option if purchase else plan.selected_option
    people = plan.group.members.filter(status='active')
    data = {'id': str(plan.id), 'title': plan.title, 'summary': plan.summary, 'share_url': plan_url(plan),
            'group_id': str(plan.group_id), 'expected_members': plan.expected_members, 'joined_count': people.count(),
            'expires_at': iso(expires_at(plan)), 'expired': not purchase and timezone.now() >= expires_at(plan),
            'checkout_ready': bool(option and plan.vote_id), 'category': option.category if option else plan.details.get('category', 'purchase'),
            'details': plan.details,
            'option': {'title': option.title, 'total_cents': option.total_cents, 'currency': option.currency,
                       'per_person_cents': -(-option.total_cents // plan.expected_members), 'features': option.features,
                       'photo_url': option.meta.get('photo_url'), 'check_in': option.meta.get('check_in'),
                       'check_out': option.meta.get('check_out'), 'cancellation': option.meta.get('cancellation', '')} if option else {
                        'title':plan.title,'total_cents':None,'per_person_cents':None,'currency':'USD','features':plan.details.get('requirements',[]),
                        'photo_url':None,'check_in':None,'check_out':None,'cancellation':''},
            'me': {'id': str(member.id), 'display_name': member.display_name, 'organizer': plan.group.organizer_id == member.id} if member else None,
            'members': [{'id': str(m.id), 'display_name': m.display_name} for m in people] if member else [],
            'purchase': {'id': str(purchase.id), 'state': purchase.state, 'deadline_at': iso(purchase.deadline_at)} if purchase else None,
            'my_share': None}
    if member and purchase:
        share = Share.objects.filter(purchase=purchase, member=member, superseded_by__isnull=True).first()
        if share:
            from payments.services import make_consent_token
            can_pay = purchase.state == 'collecting' and share.status in {'invited', 'viewing', 'card_failed'} and purchase.deadline_at > timezone.now()
            data['my_share'] = {'amount_cents': share.amount_cents, 'status': share.status,
                                'consent_url': f'/c/{make_consent_token(share)}' if can_pay else None}
    return data


def mutation_origin(request):
    origin = request.headers.get('Origin')
    target = urlparse(settings.PUBLIC_BASE_URL)
    if origin and origin != f'{target.scheme}://{target.netloc}':
        raise ApiError('invalid_origin', 'Open this plan on the Tandem website.', 403)
    if request.content_type != 'application/json':
        raise ApiError('invalid_request', 'Send JSON.', 415)


@api_view(['GET'])
@api_errors
def detail(request, plan_id):
    return Response(payload(get_object_or_404(TripPlan, pk=plan_id), request))


@api_view(['POST'])
@api_errors
@transaction.atomic
def join(request, plan_id):
    mutation_origin(request)
    plan = get_object_or_404(TripPlan.objects.select_for_update(), pk=plan_id)
    data = request.data if isinstance(request.data, dict) else {}
    name, phone = data.get('name'), data.get('phone')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or not isinstance(phone, str) or not re.fullmatch(r'\+[1-9]\d{7,14}', phone):
        raise ApiError('invalid_request', 'Enter your name and phone number with country code, such as +14155551234.')
    if data.get('accept_plan') is not True:
        raise ApiError('acceptance_required', 'Confirm that you want to join this trip plan. Payment approval happens separately.')
    phone_hash = hashlib.sha256(phone.encode()).hexdigest()
    existing = Member.objects.filter(group=plan.group, phone_e164_hash=phone_hash).first()
    if existing:
        if member_id_from_request(request) != existing.id:
            raise ApiError('already_joined', 'That number has already joined. Use the original browser to open your plan.', 409)
        response = Response(payload(plan, request))
        set_session_cookie(response, existing.id)
        return response
    if Purchase.objects.filter(group=plan.group).exists():
        raise ApiError('roster_locked', 'Payment collection has started; this trip’s participants are fixed.', 409)
    if timezone.now() >= expires_at(plan):
        raise ApiError('plan_expired', 'This plan’s join link expired. Ask the organizer for a new plan.', 410)
    is_owner = False
    if data.get('owner_token'):
        try:
            claim = signing.loads(data['owner_token'], salt=OWNER_SALT, max_age=7*86400)
        except (signing.BadSignature, TypeError):
            raise ApiError('invalid_owner_link', 'Open the organizer link from your Tandem conversation.', 403)
        if claim.get('plan') != str(plan.id) or phone_hash != plan.source_group.organizer.phone_e164_hash:
            raise ApiError('invalid_owner_link', 'Use your registered phone number with the organizer link.', 403)
        is_owner = True
    elif phone_hash == plan.source_group.organizer.phone_e164_hash:
        raise ApiError('owner_link_required', 'Open your organizer link from the private Tandem conversation.', 403)
    count = plan.group.members.filter(status='active').count()
    capacity = plan.expected_members if is_owner or plan.group.organizer_id else plan.expected_members - 1
    if count >= capacity:
        raise ApiError('plan_full', 'This plan has no remaining places.', 409)
    member = Member.objects.create(group=plan.group, display_name=name.strip(), phone_e164_hash=phone_hash,
                                   phone_last4=phone[-4:], role='organizer' if is_owner else 'member')
    if is_owner:
        plan.group.organizer = member
        plan.group.save(update_fields=['organizer'])
    if plan.vote_id:
        Ballot.objects.create(vote=plan.vote, member=member, choice_key='1')
    log_event('plan.joined', actor=f'member:{member.id}', group_id=plan.group_id, payload={'plan_id': str(plan.id)})
    # Render this member's view without accepting a client-supplied identity.
    from core.session import COOKIE_NAME, make_session_value
    request.COOKIES[COOKIE_NAME] = make_session_value(member.id)
    response = Response(payload(plan, request), status=201)
    set_session_cookie(response, member.id)
    return response


@api_view(['POST'])
@api_errors
@transaction.atomic
def fund(request, plan_id):
    mutation_origin(request)
    plan = get_object_or_404(TripPlan.objects.select_for_update(), pk=plan_id)
    member = current_member(request, plan.group_id)
    if plan.group.organizer_id != member.id:
        raise ApiError('organizer_required', 'Only the organizer can open payment collection.', 403)
    if not plan.selected_option_id or not plan.vote_id:
        raise ApiError('quote_required', 'This is a planning-only decision. A supported merchant offer is required before collecting payments.', 409)
    if not Purchase.objects.filter(group=plan.group).exists():
        if timezone.now() >= expires_at(plan):
            raise ApiError('plan_expired', 'This plan has expired.', 410)
        members = plan.group.members.filter(status='active')
        if members.count() != plan.expected_members or plan.vote.ballots.count() != plan.expected_members:
            raise ApiError('waiting_for_members', 'Everyone must join and accept the plan before payment collection opens.', 409)
        from purchases.rules import RulesEngine
        result = RulesEngine().execute('create_purchase', {'option_id': str(plan.selected_option_id),
            'member_ids': [str(m.id) for m in members], 'deadline_hours': 6}, f'member:{member.id}')
        if not result['ok']:
            raise ApiError(result['error']['code'], result['error']['message'], 409)
    return Response(payload(plan, request))
