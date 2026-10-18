import json
from unittest.mock import patch
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient
from agent.context import build_group_state
from agent.orchestrator import Orchestrator
from chat.models import ChatMessage, PhotonSession
from purchases.models import TripPlan, Purchase
from purchases.plans import owner_url
from purchases.tests.test_plans import SharedPlanTests
from urllib.parse import parse_qs, urlparse


class BuyingTests(TestCase):
    setUp = SharedPlanTests.setUp
    join = SharedPlanTests.join
    owner_join = SharedPlanTests.owner_join

    def brief(self, **changes):
        ChatMessage.objects.create(group=self.source, member=self.owner, direction='in', seq=2,
            text_redacted='Buy a gift for Rishab with 4 buyers, 30 per person')
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

    def test_arbitrary_solo_plan_has_no_fake_price_or_payments(self):
        self.brief(goal='Choose a telescope',category='telescope',group_size=1)
        result=self.engine.execute('publish_buying_plan',{},self.actor)
        self.assertTrue(result['ok'],result)
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
        self.assertEqual(self.engine.execute('publish_buying_plan',{},self.actor)['data']['plan_id'],str(self.plan.id))

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
