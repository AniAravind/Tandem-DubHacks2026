"""Private recommendations must not fall back to seeded catalog inventory."""
import os
from unittest.mock import patch
from django.test import TestCase, override_settings
from agent.context import build_group_state
from agent.orchestrator import Orchestrator
from agent.tests.helpers import reply
from purchases.models import TripPlan
from purchases.plans import plan_price, auto_price
from purchases.tests import test_plans
from purchases.tests.test_live import FakeTavily


@override_settings(PRIVATE_LIVE_ONLY=True, CACHES={'default':{'BACKEND':'django.core.cache.backends.locmem.LocMemCache'}})
class LiveOnlyTests(TestCase):
    def setUp(self):
        with override_settings(PRIVATE_LIVE_ONLY=False):
            test_plans.SharedPlanTests.setUp(self)

    def test_old_demo_options_are_hidden_and_cannot_be_recommended(self):
        self.assertEqual(build_group_state(self.source)['planning_options'],[])
        result=self.engine.execute('search_options',{'category':'rental','group_size':4},self.actor)
        self.assertEqual(result['error']['code'],'live_search_required')
        self.assertEqual(self.engine.execute('publish_plan',self.args,self.actor)['error']['code'],'live_search_required')
        result=self.engine.execute('compare_options',{'option_ids':[self.option['id']]},self.actor)
        self.assertEqual(result['error']['code'],'live_search_required')

    def test_model_can_choose_live_research_but_not_catalog_tools(self):
        def model(messages,tools,**kwargs):
            names={tool['function']['name'] for tool in tools}
            self.assertIn('live_search',names)
            self.assertIn('publish_buying_plan',names)
            self.assertNotIn('search_options',names)
            self.assertNotIn('publish_plan',names)
            return reply('Hey!')
        Orchestrator(self.source.id,llm_chat=model).run(self.actor,text='hey')

    def test_budget_is_not_a_product_price_or_checkout_quote(self):
        self.plan.details={'budget_per_person_cents':20000}
        self.plan.selected_option=None
        self.plan.save()
        self.assertIsNone(plan_price(self.plan))
        self.assertFalse(auto_price(self.plan))
        self.plan.refresh_from_db()
        self.assertIsNone(self.plan.selected_option_id)

    def test_live_links_are_retrieved_without_catalog_fallback(self):
        with patch.dict(os.environ,{'TAVILY_API_KEY':'test'}),patch('purchases.live.httpx',FakeTavily()):
            result=self.engine.execute('live_search',{'query':'cabin for four','kind':'stay','group_size':4},self.actor)
        self.assertTrue(result['ok'],result)
        orchestrator=Orchestrator(self.source.id)
        orchestrator.tool_results=[('live_search',result['data'])]
        out=orchestrator.compose(None)
        self.assertIn('https://www.vrbo.com/2023227312345678ha',out[0].text)
        self.assertNotIn('Juniper',out[0].text)

    def test_republishing_discards_unpurchased_demo_quote(self):
        from chat.models import ChatMessage
        ChatMessage.objects.create(group=self.source,member=self.owner,direction='in',seq=3,
            text_redacted='A cabin trip for 4 people, $200 each')
        saved=self.engine.execute('save_buying_brief',{'goal':'Cabin trip','category':'rental',
            'group_size':4,'budget_per_person':200},self.actor)
        self.assertTrue(saved['ok'],saved)
        self.plan.details={'revision':saved['data']['revision'],'payment_simulation':True,'price_source':'budget'}
        self.plan.save()
        result=self.engine.execute('publish_buying_plan',{},self.actor)
        self.assertTrue(result['ok'],result)
        self.plan.refresh_from_db()
        self.assertIsNone(self.plan.selected_option_id)
        self.assertNotIn('price_source',self.plan.details)
        self.assertFalse(result['data']['checkout_ready'])
