import hashlib
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from agent.tests.helpers import seed
from chat.models import ChatMessage, PhotonSession
from core.models import Group, Member
from purchases.models import Purchase, Share, TripPlan
from purchases.plans import owner_url
from purchases.rules import RulesEngine


class SharedPlanTests(TestCase):
    def setUp(self):
        seed()
        self.source = Group.objects.create(name='Private planning')
        self.phone = '+15555550001'
        self.owner = Member.objects.create(group=self.source, display_name='Planner', role='organizer',
            phone_e164_hash=hashlib.sha256(self.phone.encode()).hexdigest())
        self.source.organizer = self.owner
        self.source.save()
        PhotonSession.objects.create(key='test-planner', member=self.owner)
        ChatMessage.objects.create(group=self.source, member=self.owner, direction='in', seq=1, text_redacted='PRIVATE TRANSCRIPT DO NOT SHARE')
        self.engine = RulesEngine()
        self.actor = f'member:{self.owner.id}'
        result = self.engine.execute('search_options', {'category':'rental','group_size':4, 'budget_per_person_max':300,
            'dates': {'start':'2026-11-14','end':'2026-11-16'}}, self.actor)
        self.assertTrue(result['ok'], result)
        self.option = result['data']['options'][0]
        self.args = {'option_id':self.option['id'], 'title':'Tahoe weekend', 'summary':'Two nights in Tahoe. Arrive Friday, hike Saturday, head home Sunday.'}
        result = self.engine.execute('publish_plan', self.args, self.actor)
        self.assertTrue(result['ok'], result)
        self.plan = TripPlan.objects.get(pk=result['data']['plan_id'])
        self.path = f'/api/v1/plans/{self.plan.id}'
        self.owner_token = parse_qs(urlparse(owner_url(self.plan)).query)['owner'][0]

    def join(self, client, n=1, **extra):
        data = {'name':f'Traveler {n}', 'phone':f'+1555555000{n}', 'accept_plan':True, **extra}
        return client.post(self.path+'/join', data, format='json')

    def owner_join(self, client):
        return self.join(client, owner_token=self.owner_token)

    def test_private_search_supports_future_roster_and_publish_is_idempotent(self):
        self.assertEqual(self.source.members.count(), 1)
        self.assertEqual(self.plan.expected_members, 4)
        self.assertEqual(self.plan.group.members.count(), 0)
        self.assertEqual(self.plan.selected_option.meta['check_in'], '2026-11-14')
        result = self.engine.execute('publish_plan', self.args, self.actor)
        self.assertEqual(result['data']['plan_id'], str(self.plan.id))
        self.assertEqual(TripPlan.objects.count(), 1)
        self.assertNotIn('owner=', str(result))
        self.assertFalse(self.plan.group.messages.exists())
        self.assertFalse(self.engine.execute('start_vote', {'option_ids':[self.option['id'],self.option['id']]}, self.actor)['ok'])

    def test_public_preview_contains_no_private_chat_phone_or_payment_link(self):
        preview = APIClient().get(self.path).json()
        self.assertIsNone(preview['me'])
        self.assertIsNone(preview['my_share'])
        self.assertEqual(preview['members'], [])
        self.assertNotIn('PRIVATE TRANSCRIPT', str(preview))
        self.assertNotIn(self.phone, str(preview))

    def test_published_link_is_attached_privately_outside_llm_results(self):
        from agent.context import build_group_state
        from agent.orchestrator import Orchestrator
        from agent.tests.helpers import ScriptedLLM, reply, tool_call
        model = ScriptedLLM(reply(calls=[tool_call('publish_plan',self.args)]),reply('Your plan is ready.'))
        out = Orchestrator(self.source.id, engine=self.engine, llm_chat=model).run(self.actor)
        links = [m for m in out if m.link]
        self.assertEqual(len(links),1)
        self.assertEqual(links[0].private_to_member_id,str(self.owner.id))
        self.assertIn('?owner=',links[0].link)
        self.assertNotIn('?owner=',str(model.requests))
        self.assertEqual(build_group_state(self.source)['mode'],'private_planning')
        self.assertEqual(build_group_state(self.source)['planning_options'][0]['id'],self.option['id'])

    def test_join_needs_only_name_phone_acceptance_and_sets_cookie(self):
        client = APIClient()
        response = self.join(client, 2)
        self.assertEqual(response.status_code, 201)
        self.assertIn('tandem_session', response.cookies)
        member = Member.objects.get(pk=response.json()['me']['id'])
        self.assertIsNone(member.verified_at)
        self.assertEqual(member.phone_e164_hash, hashlib.sha256(b'+15555550002').hexdigest())
        self.assertEqual(self.plan.vote.ballots.count(), 1)
        self.assertEqual(self.join(client, 2).status_code, 200)
        self.assertEqual(self.join(APIClient(), 2).status_code, 409)

    def test_owner_requires_private_link_and_matching_phone(self):
        self.assertEqual(self.join(APIClient()).status_code, 403)
        self.assertEqual(self.join(APIClient(), 2, owner_token=self.owner_token).status_code, 403)
        self.assertEqual(self.owner_join(APIClient()).status_code, 201)
        self.plan.group.refresh_from_db()
        self.assertIsNotNone(self.plan.group.organizer_id)

    def test_demo_header_cannot_impersonate_a_joined_plan_member(self):
        joined = self.owner_join(APIClient()).json()
        stranger = APIClient()
        response = stranger.get(f'/api/v1/groups/{self.plan.group_id}', HTTP_X_TANDEM_MEMBER=joined['me']['id'])
        self.assertEqual(response.status_code,401)
        self.assertEqual(response.json()['error']['code'],'join_required')

    def test_validation_and_origin(self):
        client = APIClient()
        self.assertEqual(self.join(client, phone='123').status_code, 400)
        self.assertEqual(self.join(client, 2, accept_plan=False).status_code, 400)
        self.assertEqual(client.post(self.path+'/join', {'name':'X','phone':'+15555550002','accept_plan':True}, format='json', HTTP_ORIGIN='https://untrusted.invalid').status_code, 403)

    def test_reserves_organizer_place_and_never_closes_vote_early(self):
        from purchases.consensus import close_if_decided
        for n in [2,3,4]:
            self.assertEqual(self.join(APIClient(),n).status_code,201)
            self.assertEqual(close_if_decided(self.plan.vote).status, 'open')
        self.assertEqual(self.join(APIClient(),5).status_code,409)
        self.assertEqual(self.owner_join(APIClient()).status_code,201)

    @patch('purchases.tasks.schedule_purchase_jobs')
    def test_complete_roster_opens_one_purchase_and_individual_consent_links(self, schedule):
        owner, guest = APIClient(), APIClient()
        self.assertEqual(self.owner_join(owner).status_code,201)
        self.assertEqual(owner.post(self.path+'/fund',{},format='json').status_code,409)
        self.assertEqual(self.join(guest,2).status_code,201)
        self.assertEqual(guest.post(self.path+'/fund',{},format='json').status_code,403)
        for n in [3,4]: self.assertEqual(self.join(APIClient(),n).status_code,201)
        response = owner.post(self.path+'/fund',{},format='json')
        self.assertEqual(response.status_code,200,response.json())
        self.assertEqual(response.json()['my_share']['status'],'invited')
        self.assertTrue(response.json()['my_share']['consent_url'].startswith('/c/'))
        self.assertEqual(owner.post(self.path+'/fund',{},format='json').status_code,200)
        self.assertEqual(Purchase.objects.filter(group=self.plan.group).count(),1)
        self.assertEqual(Share.objects.filter(purchase__group=self.plan.group).count(),4)
        self.assertNotEqual(response.json()['my_share']['consent_url'],guest.get(self.path).json()['my_share']['consent_url'])
        public = APIClient().get(self.path).json()
        self.assertIsNone(public['my_share'])
        self.assertEqual(public['members'],[])
        self.assertEqual(self.join(APIClient(),5).status_code,409)

    @patch('purchases.tasks.schedule_purchase_jobs')
    def test_join_to_individual_payments_to_booking(self, schedule):
        from payments.simulated import SimulatedVisaAgentGateway
        from purchases.models import Booking
        gateway = SimulatedVisaAgentGateway(on_event=lambda *args:None, latency_ms=0, force_decline_members=set())
        clients = [APIClient() for _ in range(4)]
        self.owner_join(clients[0])
        for n, client in enumerate(clients[1:],2):
            self.assertEqual(self.join(client,n).status_code,201)
        self.assertEqual(clients[0].post(self.path+'/fund',{},format='json').status_code,200)
        with patch('payments.services.get_gateway',return_value=gateway), patch('purchases.rules.get_gateway',return_value=gateway):
            for n,client in enumerate(clients):
                consent_url = client.get(self.path).json()['my_share']['consent_url']
                endpoint = '/api/v1/consent/' + consent_url.split('/c/')[1]
                details = client.get(endpoint).json()
                self.assertEqual(details['plan_url'],f'/p/{self.plan.id}')
                card = client.post(endpoint+'/tokenize',{'transient_card_token':f'tt_sim_1111_plan{n}'},format='json').json()
                with self.captureOnCommitCallbacks(execute=True):
                    response=client.post(endpoint+'/approve',{'agent_token_id':card['agent_token_id'],
                        'cap_cents':details['default_cap_cents'],'consent_text_sha256':details['consent_text_sha256'],
                        'assertion':{'method':'otp','code':'000000'}},format='json')
                self.assertEqual(response.status_code,200,response.json())
        purchase = Purchase.objects.get(group=self.plan.group)
        self.assertEqual(purchase.state,'booked')
        self.assertEqual(Booking.objects.filter(purchase=purchase).count(),1)
        self.assertEqual(purchase.shares.filter(status='captured').count(),4)
        for client in clients:
            self.assertEqual(client.get(self.path).json()['my_share']['status'],'captured')
