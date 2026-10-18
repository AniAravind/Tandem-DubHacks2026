import hashlib
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from agent.tests.helpers import seed
from chat.models import ChatMessage, PhotonSession
from core.models import Group, Member
from purchases.models import Purchase, Share, TripPlan
from purchases.plans import owner_url
from purchases.rules import RulesEngine


@override_settings(PRIVATE_LIVE_ONLY=False)
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

    def test_private_agent_tools_are_small_and_published_link_survives_narration_failure(self):
        from agent.orchestrator import Orchestrator
        from agent.tests.helpers import reply, tool_call
        count = 0
        def llm(messages,tools,**kwargs):
            nonlocal count
            self.assertEqual({t['function']['name'] for t in tools},{'search_options','publish_plan','save_buying_brief','publish_buying_plan',
                                                                    'live_search','compare_options','remember_preference'})
            count += 1
            if count == 1:
                return reply(calls=[tool_call('publish_plan',self.args)])
            raise TimeoutError('Narration unavailable')
        out = Orchestrator(self.source.id,engine=self.engine,llm_chat=llm).run(self.actor)
        self.assertTrue(any(m.link and '?owner=' in m.link for m in out))

    def test_private_context_excludes_obsolete_assistant_claims(self):
        from agent.context import build_messages
        from agent.orchestrator import SYSTEM_PROMPT
        ChatMessage.objects.create(group=self.source,direction='out',seq=2,text_redacted='FALSE ORGANIZER LINK. Only one member means a search cannot run.')
        messages=build_messages(self.source,SYSTEM_PROMPT)
        self.assertNotIn('FALSE ORGANIZER LINK',str(messages))
        self.assertIn('PRIVATE TRANSCRIPT DO NOT SHARE',str(messages))
        self.assertTrue(any('NEVER blocks a search' in m['content'] for m in messages if m['role']=='system'))
        self.assertEqual(messages[-1]['role'],'user')
        self.assertIn('Current user message',messages[-1]['content'])

    def test_false_link_claim_triggers_publication_and_only_server_link_narration(self):
        from agent.orchestrator import Orchestrator
        from agent.tests.helpers import ScriptedLLM,reply,tool_call
        model=ScriptedLLM(reply('Here’s the private organizer link — share it with friends.'),
                          reply(calls=[tool_call('publish_plan',self.args)]),
                          reply('Share the private organizer link with all your friends.'))
        out=Orchestrator(self.source.id,engine=self.engine,llm_chat=model).run(self.actor)
        self.assertTrue(any(m.link and '?owner=' in m.link for m in out))
        self.assertNotIn('Share the private organizer link',str([m.text for m in out]))
        self.assertIn('No plan link has been created',model.requests[1][-1]['content'])

    def test_unrepaired_false_link_claim_is_not_delivered(self):
        from agent.orchestrator import Orchestrator
        from agent.tests.helpers import ScriptedLLM,reply
        model=ScriptedLLM(*[reply('Here is the private organizer link.') for _ in range(3)])
        out=Orchestrator(self.source.id,engine=self.engine,llm_chat=model).run(self.actor)
        self.assertEqual(len(out),1)
        self.assertIn('haven’t created a buying plan',out[0].text)
        self.assertIsNone(out[0].link)

    def test_private_single_member_search_block_is_repaired(self):
        from agent.orchestrator import Orchestrator
        from agent.tests.helpers import ScriptedLLM,reply,tool_call
        model=ScriptedLLM(reply('I see only you, so a 4-person search can’t run yet.'),
            reply(calls=[tool_call('search_options',{'category':'rental','group_size':4,'budget_per_person_max':300})]),reply('Here are the options.'))
        out=Orchestrator(self.source.id,engine=self.engine,llm_chat=model).run(self.actor)
        self.assertEqual(sum(m.card is not None and m.card.type=='OptionCard' for m in out),3)

    def test_add_resends_real_saved_proposal_without_calling_model(self):
        from agent.orchestrator import Orchestrator
        def no_llm(*args,**kwargs):
            raise AssertionError('Saved options are sufficient for an explicit Add/link request')
        out=Orchestrator(self.source.id,engine=self.engine,llm_chat=no_llm).run(self.actor,text='Add')
        self.assertTrue(any(m.link and '?owner=' in m.link for m in out))
        out=Orchestrator(self.source.id,engine=self.engine,llm_chat=no_llm).run(self.actor,text='Send me the organizer link')
        self.assertTrue(any(m.link and '?owner=' in m.link for m in out))
        self.assertEqual(TripPlan.objects.filter(source_group=self.source).count(),1)

    def test_explicit_invite_request_publishes_after_search_even_if_model_stops(self):
        from agent.orchestrator import Orchestrator
        from agent.tests.helpers import ScriptedLLM,reply,tool_call
        model=ScriptedLLM(reply(calls=[tool_call('search_options',{'category':'rental','group_size':4,'budget_per_person_max':300})]),reply('Which place would you like?'))
        out=Orchestrator(self.source.id,engine=self.engine,llm_chat=model).run(self.actor,text='Create a group invite link for 4 travelers after searching')
        self.assertTrue(any(m.link and '?owner=' in m.link for m in out))
        self.assertEqual(TripPlan.objects.filter(source_group=self.source).count(),2)

    def test_resend_keeps_the_already_chosen_plan(self):
        from agent.orchestrator import Orchestrator
        option=self.source.options.exclude(pk=self.option['id']).first()
        chosen=self.engine.execute('publish_plan',{'option_id':str(option.id),'title':'Our chosen place','summary':'The organizer chose this option.'},self.actor)['data']['plan_id']
        out=Orchestrator(self.source.id,engine=self.engine,llm_chat=lambda *args,**kwargs:None).run(self.actor,text='Send me the organizer link')
        self.assertTrue(any(m.link and f'/p/{chosen}?' in m.link for m in out))
        self.assertEqual(TripPlan.objects.filter(source_group=self.source).count(),2)

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

    @patch('purchases.tasks.schedule_purchase_jobs')
    def test_final_join_automatically_prepares_private_payment_links(self, schedule):
        clients = [APIClient() for _ in range(4)]
        self.owner_join(clients[0])
        for n, client in enumerate(clients[1:], 2):
            response = self.join(client, n)
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()['my_share']['consent_url'])
        self.assertEqual(Purchase.objects.filter(group=self.plan.group).count(), 1)
        links = [client.get(self.path).json()['my_share']['consent_url'] for client in clients]
        self.assertEqual(len(set(links)), 4)
        self.assertIsNone(APIClient().get(self.path).json()['my_share'])
        self.assertEqual(clients[0].post(self.path+'/fund', {}, format='json').status_code, 200)
        self.assertEqual(Purchase.objects.filter(group=self.plan.group).count(), 1)

    def test_us_phone_formats_use_same_canonical_identity(self):
        client = APIClient()
        response = self.join(client, 2, phone='(555) 555-0002')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.join(client, 2, phone='1 555 555 0002').status_code, 200)
        self.assertEqual(self.join(APIClient(), 2, phone='+1 555 555 0002').status_code, 409)
        self.assertEqual(self.join(APIClient(), 3, phone='5555550003').status_code, 201)
        self.assertEqual(self.join(APIClient(), 4, phone='+44 7700 900123').status_code, 201)

    @patch('purchases.tasks.schedule_purchase_jobs')
    def test_planning_only_payment_simulation_requires_explicit_owner_total(self, schedule):
        self.plan.selected_option = None
        self.plan.vote = None
        self.plan.save()
        owner = APIClient()
        self.owner_join(owner)
        guests = [APIClient() for _ in range(3)]
        for n, client in enumerate(guests, 2):
            self.assertEqual(self.join(client, n).status_code, 201)
        self.assertFalse(Purchase.objects.filter(group=self.plan.group).exists())
        self.assertEqual(owner.post(self.path+'/fund', {}, format='json').status_code, 409)
        data = {'confirm_simulation':True, 'demo_total_cents':12000}
        self.assertEqual(guests[0].post(self.path+'/fund', data, format='json').status_code, 403)
        for total in [True, 0, -100, 800001]:
            self.assertEqual(owner.post(self.path+'/fund', {**data,'demo_total_cents':total}, format='json').status_code, 400)
        with self.settings(DEMO_MODE=False):
            self.assertEqual(owner.post(self.path+'/fund', data, format='json').status_code, 409)
        response = owner.post(self.path+'/fund', data, format='json')
        self.assertEqual(response.status_code, 200, response.json())
        self.assertTrue(response.json()['payment_simulation'])
        self.assertEqual(response.json()['my_share']['amount_cents'], 3000)
        self.assertTrue(response.json()['my_share']['consent_url'])
        self.assertEqual(owner.post(self.path+'/fund', data, format='json').status_code, 200)
        self.assertEqual(Purchase.objects.filter(group=self.plan.group).count(), 1)
        from payments.simulated import SimulatedVisaAgentGateway
        gateway = SimulatedVisaAgentGateway(on_event=lambda *args:None, latency_ms=0, force_decline_members=set())
        with patch('payments.services.get_gateway',return_value=gateway), patch('purchases.rules.get_gateway',return_value=gateway):
            for n, client in enumerate([owner, *guests]):
                url = client.get(self.path).json()['my_share']['consent_url']
                endpoint = '/api/v1/consent/' + url.split('/c/')[1]
                consent = client.get(endpoint).json()
                card = client.post(endpoint+'/tokenize', {'transient_card_token':f'tt_sim_1111_demo{n}'}, format='json').json()
                with self.captureOnCommitCallbacks(execute=True):
                    approved = client.post(endpoint+'/approve', {'agent_token_id':card['agent_token_id'],
                        'cap_cents':consent['default_cap_cents'], 'consent_text_sha256':consent['consent_text_sha256'],
                        'assertion':{'method':'otp','code':'000000'}}, format='json')
                self.assertEqual(approved.status_code, 200, approved.json())
        self.assertEqual(Purchase.objects.get(group=self.plan.group).state, 'booked')
        self.assertEqual(owner.get(self.path).json()['my_share']['status'], 'captured')

    def test_dashboard_workspace_includes_only_the_joined_groups_saved_plan(self):
        owner=APIClient()
        self.owner_join(owner)
        self.plan.details={**self.plan.details,'plan_sections':[{'heading':'Itinerary','body':'Suggestion: hike Saturday.'}],
            'live_offers':[{'offer_id':'saved-source','kind':'stay','url':'https://example.com/stay','title':'Saved hotel','source':'example.com','snippet':'Source details'}]}
        self.plan.save()
        url=f'/api/v1/groups/{self.plan.group_id}?include=workspace'
        response=owner.get(url)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['plan']['details']['live_offers'][0]['url'],'https://example.com/stay')
        self.assertEqual(response.json()['plan']['details']['plan_sections'][0]['heading'],'Itinerary')
        self.assertEqual(APIClient().get(url).status_code,401)
        self.assertNotIn('PRIVATE TRANSCRIPT',str(response.json()))

    @patch('purchases.rules._notify_dropout')
    @patch('purchases.tasks.schedule_purchase_jobs')
    def test_withdrawal_updates_plan_roster_and_preserves_other_approved_amounts(self, schedule, notify):
        from purchases.rules import member_withdraw
        owner, guest = APIClient(), APIClient()
        self.owner_join(owner)
        guest_data=self.join(guest,2).json()
        for n in [3,4]: self.join(APIClient(),n)
        before=owner.get(self.path).json()
        share=Share.objects.get(purchase__group=self.plan.group,member_id=guest_data['me']['id'])
        result=member_withdraw(share.id,'member_left')
        self.assertEqual(result['share_status'],'declined')
        updated=owner.get(self.path).json()
        self.assertEqual(updated['joined_count'],3)
        departed=next(m for m in updated['members'] if m['id']==guest_data['me']['id'])
        self.assertTrue(departed['left'])
        self.assertEqual(departed['share_status'],'declined')
        self.assertEqual(updated['my_share']['amount_cents'],before['my_share']['amount_cents'])
        self.assertEqual(guest.get(self.path).json()['my_share']['status'],'declined')
