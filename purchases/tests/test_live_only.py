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


@patch('purchases.live.check_sources',new=lambda rows:{row['offer_id']:{'actionable':True,'reason':'Fixture source'} for row in rows})
@patch('purchases.recommendations.classify',new=lambda rows:{row['offer_id']:{'actionable':True,'reason':'Fixture property'} for row in rows})
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
        selected=self.engine.execute('present_recommendations',{'selections':[{'offer_id':result['data']['offers'][0]['offer_id'],'evidence_index':1}]},self.actor)
        self.assertTrue(selected['ok'],selected)
        orchestrator.tool_results=[('live_search',result['data']),('present_recommendations',selected['data'])]
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

    def test_selection_rejects_invented_features_and_unknown_sources(self):
        with patch.dict(os.environ,{'TAVILY_API_KEY':'test'}),patch('purchases.live.httpx',FakeTavily()):
            result=self.engine.execute('live_search',{'query':'cabin','kind':'stay','group_size':4},self.actor)
        offer=result['data']['offers'][0]
        args={'selections':[{'offer_id':offer['offer_id'],'evidence_index':1,'evidence':'Invented private helicopter'}]}
        self.assertEqual(self.engine.execute('present_recommendations',args,self.actor)['error']['code'],'invalid_arguments')
        args['selections'][0]={'offer_id':'00000000-0000-0000-0000-000000000000','evidence_index':1}
        self.assertEqual(self.engine.execute('present_recommendations',args,self.actor)['error']['code'],'unknown_offer')
        args['selections'][0]={'offer_id':offer['offer_id'],'evidence_index':1}
        args['summary']='This costs $60 total.'
        self.assertEqual(self.engine.execute('present_recommendations',args,self.actor)['error']['code'],'invalid_arguments')

    def test_curated_letters_select_the_exact_source_across_searches(self):
        from chat.models import PhotonSession
        from agent.private_actions import current_live_offers
        from purchases import live
        from chat.models import ChatMessage
        ChatMessage.objects.create(group=self.source,member=self.owner,direction='in',seq=3,text_redacted='A cabin for 4 people, $200 each')
        self.assertTrue(self.engine.execute('save_buying_brief',{'goal':'Cabin trip','category':'rental','group_size':4,'budget_per_person':200},self.actor)['ok'])
        with patch.dict(os.environ,{'TAVILY_API_KEY':'test'}),patch('purchases.live.httpx',FakeTavily()):
            first=self.engine.execute('live_search',{'query':'cabin','kind':'stay','group_size':4},self.actor)
            second=self.engine.execute('live_search',{'query':'hiking provider','kind':'activity','group_size':4},self.actor)
        selected=self.engine.execute('present_recommendations',{'selections':[{'offer_id':first['data']['offers'][0]['offer_id'],'evidence_index':1},
                          {'offer_id':second['data']['offers'][1]['offer_id'],'evidence_index':1}]},self.actor)
        self.assertTrue(selected['ok'],selected)
        session=PhotonSession.objects.get(member=self.owner)
        self.assertEqual(str(current_live_offers(self.source,session)[0].id),first['data']['offers'][0]['offer_id'])
        compared=self.engine.execute('compare_options',{'offer_labels':['A','B']},self.actor)
        self.assertTrue(compared['ok'],compared)
        self.assertEqual([row['id'] for row in compared['data']['rows']],[first['data']['offers'][0]['offer_id'],second['data']['offers'][1]['offer_id']])
        out=Orchestrator(self.source.id,llm_chat=lambda *args,**kw: self.fail('Selection should use the saved shortlist')).run(self.actor,text='A')
        plan=TripPlan.objects.get(pk=next(m.link for m in out if m.link).split('/p/')[1].split('?')[0])
        self.assertEqual(plan.details['featured_offer_id'],first['data']['offers'][0]['offer_id'])

    def test_curated_reply_is_one_shortlist_and_does_not_request_more_narration(self):
        from agent.tests.helpers import reply,tool_call
        import json
        calls=[]
        def model(messages,tools,**kwargs):
            calls.append(1)
            if len(calls)==1:
                return reply(calls=[tool_call('live_search',{'query':'internal search query','kind':'stay','group_size':4})])
            if len(calls)==2:
                result=json.loads(next(m['content'] for m in reversed(messages) if m['role']=='tool'))
                return reply(calls=[tool_call('present_recommendations',{'selections':[{'offer_id':result['data']['offers'][0]['offer_id'],'evidence_index':1}]})])
            self.fail('A completed presentation does not need another AI call')
        with patch.dict(os.environ,{'TAVILY_API_KEY':'test'}),patch('purchases.live.httpx',FakeTavily()):
            out=Orchestrator(self.source.id,llm_chat=model).run(self.actor,text='Find cabins')
        self.assertEqual(len(calls),2)
        self.assertIn('Sleeps 6.',out[0].text)
        self.assertNotIn('internal search query',out[0].text)
        self.assertNotIn('Tahoe cabins guide',out[0].text)
        self.assertEqual(out[0].text.count('Check final prices, availability'),1)
        self.assertEqual(sum(m.card is not None for m in out),1)

    def test_guide_and_news_pages_cannot_be_published_as_recommendations(self):
        with patch.dict(os.environ,{'TAVILY_API_KEY':'test'}),patch('purchases.live.httpx',FakeTavily()):
            result=self.engine.execute('live_search',{'query':'cabin','kind':'stay','group_size':4},self.actor)
        guide=result['data']['offers'][-1]
        with patch('purchases.recommendations.classify',return_value={guide['offer_id']:{'actionable':False,'reason':'General guide, not a property'}}):
            selected=self.engine.execute('present_recommendations',{'selections':[{'offer_id':guide['offer_id'],'evidence_index':0}]},self.actor)
        self.assertEqual(selected['error']['code'],'not_actionable_source')

    def test_shortlist_can_retain_an_option_from_an_earlier_query(self):
        with patch.dict(os.environ,{'TAVILY_API_KEY':'test'}),patch('purchases.live.httpx',FakeTavily()):
            first=self.engine.execute('live_search',{'query':'cabin first query','kind':'stay','group_size':4},self.actor)
            self.engine.execute('live_search',{'query':'cabin refined query','kind':'stay','group_size':4},self.actor)
        chosen=first['data']['offers'][0]['offer_id']
        selected=self.engine.execute('present_recommendations',{'selections':[{'offer_id':chosen,'evidence_index':1}]},self.actor)
        self.assertTrue(selected['ok'],selected)
        from purchases import live
        self.assertEqual(str(live.for_plan(self.source,None)[0].id),chosen)

    def test_bad_source_does_not_hide_valid_options_or_claim_a_complete_package(self):
        from chat.models import PhotonSession
        session=PhotonSession.objects.get(member=self.owner)
        session.planning_state={'research_kinds':['flight','stay','activity']}
        session.save()
        with patch.dict(os.environ,{'TAVILY_API_KEY':'test'}),patch('purchases.live.httpx',FakeTavily()):
            result=self.engine.execute('live_search',{'query':'cabin','kind':'stay','group_size':4},self.actor)
        good,guide=result['data']['offers'][0],result['data']['offers'][-1]
        verdicts={good['offer_id']:{'actionable':True,'reason':'Property'},guide['offer_id']:{'actionable':False,'reason':'Editorial guide'}}
        with patch('purchases.recommendations.classify',return_value=verdicts):
            result=self.engine.execute('present_recommendations',{'selections':[{'offer_id':good['offer_id'],'evidence_index':1},{'offer_id':guide['offer_id'],'evidence_index':0}]},self.actor)
        self.assertTrue(result['ok'],result)
        self.assertEqual(len(result['data']['offers']),1)
        self.assertEqual(result['data']['missing_components'],['activity','flight'])

    def test_editorial_search_hits_are_filtered_before_the_planner_sees_them(self):
        def classify(rows):
            return {row['offer_id']:{'actionable':'guide' not in row['title'].lower(),'reason':'Page purpose'} for row in rows}
        with patch.dict(os.environ,{'TAVILY_API_KEY':'test'}),patch('purchases.live.httpx',FakeTavily()),patch('purchases.live.check_sources',side_effect=classify):
            result=self.engine.execute('live_search',{'query':'cabin','kind':'stay','group_size':4},self.actor)
        self.assertTrue(result['ok'],result)
        self.assertFalse(any('guide' in row['title'].lower() for row in result['data']['offers']))
        self.assertFalse(any('guide' in row['title'].lower() for row in build_group_state(self.source)['research_results']))

    def test_undated_flight_ads_do_not_look_like_fares_for_the_requested_dates(self):
        from agent import cards
        data={'curated':True,'search_id':'shortlist','query':'','disclaimer':'unverified','offers':[{
            'offer_id':'test','label':'A','kind':'flight','title':'$255 Flights in December','source':'airline.example',
            'url':'https://airline.example/sea/hnl','evidence':'Depart December 7, from $255',
            'price_cents':25500,'price_unit':'listed','price_text':'$255','est_per_person_cents':None}]}
        text=cards.recommendations_text(data)
        self.assertIn('https://airline.example/sea/hnl',text)
        self.assertNotIn('$255',text)
        self.assertNotIn('December 7',text)
        self.assertIn('Flight fares for your travel dates have not been verified',text)
        card=cards.live_offers_card(data)
        self.assertIsNone(card.data['offers'][0]['price_cents'])
