import json
from unittest.mock import patch
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from agent.context import build_group_state
from agent.orchestrator import Orchestrator
from chat.models import ChatMessage, PhotonSession
from purchases.models import TripPlan, Purchase
from purchases.plans import owner_url
from purchases.tests.test_plans import SharedPlanTests
from urllib.parse import parse_qs, urlparse


@override_settings(PRIVATE_LIVE_ONLY=False)
class BuyingTests(TestCase):
    setUp = SharedPlanTests.setUp
    join = SharedPlanTests.join
    owner_join = SharedPlanTests.owner_join

    def brief(self, **changes):
        ChatMessage.objects.create(group=self.source, member=self.owner, direction='in', seq=2,
            text_redacted=f"Buy a gift for Rishab with {changes.get('group_size',4)} buyers, 30 per person")
        args = {'goal':'Gift for Rishab','category':'gift_bundle','group_size':4,'budget_per_person':30,
                'summary':'Choose a thoughtful birthday gift.','recommendations':['Choose a shared experience or a gift based on his hobbies.'], **changes}
        result = self.engine.execute('save_buying_brief',args,self.actor)
        self.assertTrue(result['ok'],result)
        return result['data']

    def test_goal_switch_filters_old_quotes_and_rejects_stale_publication(self):
        self.brief()
        self.assertEqual(build_group_state(self.source)['planning_options'],[])
        self.assertEqual(self.engine.execute('publish_plan',self.args,self.actor)['error']['code'],'stale_option')
        self.assertTrue(Orchestrator(self.source.id).check_output('Your budget is $30 per person.')[1] is None)
        self.assertEqual(Orchestrator(self.source.id).check_output('It costs $123.45.')[1],'invented_amount')

    def test_persisted_verified_prices_do_not_trigger_menu_loop(self):
        o=Orchestrator(self.source.id)
        self.assertIsNone(o.check_output('Juniper Loft is $231.25 each and $925.00 total.')[1])

    def test_solo_plan_is_priced_from_the_confirmed_budget(self):
        self.brief(goal='Choose a telescope',category='telescope',group_size=1)       # brief() confirms $30 per buyer
        result=self.engine.execute('publish_buying_plan',{},self.actor)
        self.assertTrue(result['ok'],result)
        self.assertTrue(result['data']['checkout_ready'])
        self.plan=TripPlan.objects.get(pk=result['data']['plan_id'])
        preview=APIClient().get(f'/api/v1/plans/{self.plan.id}').json()
        self.assertTrue(preview['checkout_ready'])
        self.assertEqual(preview['option']['total_cents'],3000)
        self.assertIn('$30 per-person budget', preview['price_source'])
        self.assertEqual(self.engine.execute('publish_buying_plan',{},self.actor)['data']['plan_id'],str(self.plan.id))
        self.assertEqual(TripPlan.objects.get(pk=self.plan.pk).selected_option.total_cents,3000)   # republish keeps one price

    def test_plan_without_price_or_budget_stays_unpriced(self):
        ChatMessage.objects.create(group=self.source, member=self.owner, direction='in', seq=2, text_redacted='Help me pick a telescope, just me')
        result=self.engine.execute('save_buying_brief',{'goal':'Choose a telescope','category':'telescope','group_size':1},self.actor)
        self.assertTrue(result['ok'],result)
        result=self.engine.execute('publish_buying_plan',{},self.actor)
        self.plan=TripPlan.objects.get(pk=result['data']['plan_id'])
        self.path=f'/api/v1/plans/{self.plan.id}'
        self.owner_token=parse_qs(urlparse(owner_url(self.plan)).query)['owner'][0]
        client=APIClient()
        preview=client.get(self.path).json()
        self.assertFalse(preview['checkout_ready'])
        self.assertIsNone(preview['option']['total_cents'])
        self.assertEqual(self.owner_join(client).status_code,201)
        self.assertEqual(client.post(self.path+'/fund',{},format='json').json()['error']['code'],'quote_required')
        self.assertFalse(Purchase.objects.filter(group=self.plan.group).exists())

    def test_budget_and_recommendations_cannot_invent_prices(self):
        self.brief()
        args={'goal':'Gift','category':'gift_bundle','group_size':4,'budget_per_person':999}
        self.assertEqual(self.engine.execute('save_buying_brief',args,self.actor)['error']['code'],'budget_not_confirmed')
        args.pop('budget_per_person');args['recommendations']=['Buy this for $25']
        self.assertEqual(self.engine.execute('save_buying_brief',args,self.actor)['error']['code'],'unverified_offer')

    @patch('purchases.tasks.schedule_purchase_jobs')
    def test_solo_gift_opens_one_real_engine_share(self, schedule):
        call_command('seed_buying_catalog',verbosity=0)
        self.brief(group_size=1)
        result=self.engine.execute('search_options',{'category':'gift_bundle','group_size':1,'budget_per_person_max':30},self.actor)
        self.assertTrue(result['ok'],result)
        self.assertEqual(len(result['data']['options']),1)
        option=result['data']['options'][0]
        result=self.engine.execute('publish_plan',{'option_id':option['id'],'title':'Solo gift','summary':'A simulated gift purchase.'},self.actor)
        self.assertTrue(result['ok'],result)
        self.plan=TripPlan.objects.get(pk=result['data']['plan_id'])
        self.path=f'/api/v1/plans/{self.plan.id}'
        self.owner_token=parse_qs(urlparse(owner_url(self.plan)).query)['owner'][0]
        client=APIClient();self.assertEqual(self.owner_join(client).status_code,201)
        response=client.post(self.path+'/fund',{},format='json')
        self.assertEqual(response.status_code,200,response.json())
        purchase=Purchase.objects.get(group=self.plan.group)
        self.assertEqual(purchase.shares.count(),1)
        self.assertEqual(purchase.total_cents,2499)

        from payments.simulated import SimulatedVisaAgentGateway
        gateway=SimulatedVisaAgentGateway(on_event=lambda *a:None,latency_ms=0,force_decline_members=set())
        with patch('payments.services.get_gateway',return_value=gateway),patch('purchases.rules.get_gateway',return_value=gateway):
            endpoint='/api/v1/consent/'+client.get(self.path).json()['my_share']['consent_url'].split('/c/')[1]
            details=client.get(endpoint).json()
            card=client.post(endpoint+'/tokenize',{'transient_card_token':'tt_sim_1111_solo'},format='json').json()
            with self.captureOnCommitCallbacks(execute=True):
                response=client.post(endpoint+'/approve',{'agent_token_id':card['agent_token_id'],'cap_cents':details['default_cap_cents'],
                    'consent_text_sha256':details['consent_text_sha256'],'assertion':{'method':'otp','code':'000000'}},format='json')
            self.assertEqual(response.status_code,200,response.json())
        purchase.refresh_from_db()
        self.assertEqual(purchase.state,'booked')
        self.assertEqual(purchase.shares.filter(status='captured').count(),1)

    def test_group_gift_completes_four_separate_payments(self):
        call_command('seed_buying_catalog',verbosity=0)
        self.brief()
        result=self.engine.execute('search_options',{'category':'gift_bundle','group_size':4,'budget_per_person_max':30},self.actor)
        self.assertTrue(result['ok'],result)
        result=self.engine.execute('publish_plan',{'option_id':result['data']['options'][0]['id'],'title':'Group gift','summary':'A simulated gift order.'},self.actor)
        self.plan=TripPlan.objects.get(pk=result['data']['plan_id'])
        self.path=f'/api/v1/plans/{self.plan.id}'
        self.owner_token=parse_qs(urlparse(owner_url(self.plan)).query)['owner'][0]
        SharedPlanTests.test_join_to_individual_payments_to_booking(self)

    def test_explicit_headcounts_override_model_interpretation(self):
        from purchases.briefs import explicit_buyer_count
        self.assertEqual(explicit_buyer_count('splitting with 4 people total'),4)
        self.assertEqual(explicit_buyer_count('For me and 4 friends'),5)
        self.assertEqual(explicit_buyer_count('for myself'),1)
        self.brief()
        result=self.engine.execute('save_buying_brief',{'goal':'Gift','category':'gift_bundle','group_size':5},self.actor)
        self.assertEqual(result['error']['code'],'buyer_count_mismatch')

    def test_explicit_combined_budget_is_saved_per_buyer_without_increasing_total(self):
        text = 'I am splitting with another friend and we have a combined budget of $60 dollars. Give me gift ideas.'
        ChatMessage.objects.create(group=self.source, member=self.owner, direction='in', seq=2, text_redacted=text)
        args = {'goal':'Birthday gift','category':'gift_bundle','group_size':2,'budget_total':60,'new_goal':True}
        result = self.engine.execute('save_buying_brief', args, self.actor)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['data']['budget_per_person_cents'],3000)
        self.assertEqual(result['data']['budget_total_cents'],6000)
        self.assertEqual(result['data']['group_size'],2)
        self.assertEqual(self.engine.execute('save_buying_brief',{**args,'new_goal':False,'budget_per_person':60},self.actor)['error']['code'],'budget_mismatch')

    def test_per_buyer_budget_is_not_divided_and_total_preserves_odd_cent(self):
        args = {'goal':'Shared equipment','category':'electronics','group_size':2,'new_goal':True}
        ChatMessage.objects.create(group=self.source,member=self.owner,direction='in',seq=2,text_redacted='Equipment for 2 buyers, $60 each')
        result = self.engine.execute('save_buying_brief',{**args,'budget_per_person':60},self.actor)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['data']['budget_per_person_cents'],6000)
        self.assertEqual(result['data']['budget_total_cents'],12000)
        ChatMessage.objects.create(group=self.source,member=self.owner,direction='in',seq=3,text_redacted='Our new budget is $60.01 total for 2 buyers')
        result = self.engine.execute('save_buying_brief',{**args,'budget_total':60.01},self.actor)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['data']['budget_per_person_cents'],3000)
        self.assertEqual(result['data']['budget_total_cents'],6001)

    def test_model_inferred_count_requires_real_source_evidence(self):
        text = 'My roommate and I are sharing equipment. The total budget is $120.'
        ChatMessage.objects.create(group=self.source,member=self.owner,direction='in',seq=2,text_redacted=text)
        args = {'goal':'Shared equipment','category':'electronics','group_size':2,'budget_total':120,'new_goal':True,
                'group_size_evidence':'My roommate and I'}
        result = self.engine.execute('save_buying_brief',args,self.actor)
        self.assertTrue(result['ok'],result)
        state = PhotonSession.objects.get(member=self.owner).planning_state
        self.assertTrue(state['_buyer_count_confirmed'])
        self.assertEqual(state['budget_per_person_cents'],6000)
        result = self.engine.execute('save_buying_brief',{**args,'new_goal':False,'group_size':3,'group_size_evidence':'an invented phrase'},self.actor)
        self.assertEqual(result['error']['code'],'buyer_count_evidence_missing')

    def test_new_goal_can_be_recovered_from_cited_request_after_followups(self):
        self.brief()
        ChatMessage.objects.create(group=self.source,member=self.owner,direction='in',seq=3,
            text_redacted='Plan a trip for me and 4 friends to Mt Rainier. 200 dollars a person. 10/20-10/22.')
        ChatMessage.objects.create(group=self.source,member=self.owner,direction='in',seq=4,text_redacted='Seattle')
        args = {'goal':'Mt Rainier trip','category':'travel','group_size':5,'budget_per_person':200,
            'new_goal':True,'source_message_seq':3,'decision_details':[{'topic':'Origin','value':'Seattle'}]}
        result=self.engine.execute('save_buying_brief',args,self.actor)
        self.assertTrue(result['ok'],result)
        state=PhotonSession.objects.get(member=self.owner).planning_state
        self.assertEqual(state['group_size'],5)
        self.assertEqual(state['budget_per_person_cents'],20000)
        self.assertTrue(state['_buyer_count_confirmed'])
        self.assertNotIn('gift',state['goal'].lower())
        self.assertEqual(self.engine.execute('save_buying_brief',{**args,'source_message_seq':999},self.actor)['error']['code'],'request_source_missing')
        self.assertEqual(self.engine.execute('save_buying_brief',{**args,'source_message_seq':2},self.actor)['error']['code'],'stale_request_source')
        self.assertFalse(self.engine.execute('save_buying_brief',{**args,'source_message_seq':4,'budget_per_person':999},self.actor)['ok'])
