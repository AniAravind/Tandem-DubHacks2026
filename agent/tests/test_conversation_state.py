"""Regression for the real China transcript: clarification, outages, short answers and invalid fares."""
import hashlib
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.core.cache import cache

from agent.context import build_group_state, build_messages
from agent.dialogue import question_type
from agent.llm import ProviderUnavailable
from agent.orchestrator import Orchestrator
from agent.safety import planning_amounts_in
from agent.tests.helpers import reply, tool_call, ScriptedLLM
from chat.adapters.base import AgentMessage, InboundMessage
from chat.models import ChatMessage, PhotonSession
from chat import services
from core.models import Group, Member
from purchases.models import TripPlan, LiveOffer
from purchases.rules import RulesEngine
from purchases import live
from purchases.tests.test_live import FakeTavily


@override_settings(CACHES={'default':{'BACKEND':'django.core.cache.backends.locmem.LocMemCache','LOCATION':'conversation-state'}})
class ConversationStateTests(TestCase):
    def setUp(self):
        cache.clear()
        self.group=Group.objects.create(name='Private agent',channel='imessage')
        self.member=Member.objects.create(group=self.group,display_name='Planner',role='organizer',phone_e164_hash=hashlib.sha256(b'test').hexdigest())
        self.group.organizer=self.member
        self.group.save()
        self.session=PhotonSession.objects.create(key='china-regression',member=self.member)
        self.engine=RulesEngine()
        self.actor=f'member:{self.member.id}'
        self.input('China trip for me and 3 friends. Budget 1000 a person. 10/20 - 10/27')
        self.assertTrue(self.save(budget_per_person=1000)['ok'])

    def input(self,text):
        return services.store_inbound(InboundMessage(str(self.group.id),str(self.member.id),text,'imessage'))

    def save(self,**extra):
        return self.engine.execute('save_buying_brief',{'goal':'China trip','category':'travel','group_size':4,**extra},self.actor)

    def state(self):
        self.session.refresh_from_db()
        return self.session.planning_state

    def ask(self,text):
        services.post_card(self.group.id,AgentMessage(text=text))

    def test_budget_phrases_and_partial_updates_preserve_four_travelers(self):
        self.assertEqual(planning_amounts_in('1000 a person'),{100000})
        self.assertEqual(planning_amounts_in('1,000 per traveler'),{100000})
        self.assertEqual(planning_amounts_in('300'),set())
        self.ask('Where will you be flying from?')
        self.input('Seattle Tacoma Airport')
        self.assertTrue(self.save(goal='Book flight to China',decision_details=[{'topic':'Origin','value':'Seattle Tacoma Airport (SEA)'}])['ok'])
        self.ask('What city in China would you like to visit?')
        self.input('Guangzhou')
        self.assertTrue(self.save(decision_details=[{'topic':'Destination','value':'Guangzhou (CAN)'}])['ok'])
        self.assertEqual(self.state()['budget_per_person_cents'],100000)
        self.assertEqual(self.state()['group_size'],4)
        self.assertEqual(self.state()['goal'],'China trip')
        self.assertEqual(len(self.state()['decision_details']),2)
        self.assertFalse(self.save(group_size=1)['ok'])
        self.assertEqual(self.state()['group_size'],4)

    def test_yes_survives_provider_outages_and_cannot_reset_the_goal(self):
        self.ask('Do you also need hotel accommodations for this trip?')
        msg=self.input('Yes')
        with patch('agent.llm.chat',side_effect=ProviderUnavailable(120)):
            services.run_agent(str(self.group.id),str(self.member.id),message_id=str(msg.id))
        self.assertIn('hotel accommodations',self.state()['_dialogue']['question'])
        self.assertEqual(self.state()['_dialogue']['answer'],'Yes')
        self.input('Yes')
        self.assertFalse(self.save(new_goal=True,group_size=1,goal='Book a flight')['ok'])
        self.assertTrue(self.save(research_kinds=['flight','stay','activity'])['ok'])
        context=build_group_state(self.group)['conversation_context']
        self.assertIn('hotel accommodations',context['question'])
        self.assertEqual(context['answer'],'Yes')
        self.assertEqual(self.state()['budget_per_person_cents'],100000)
        self.assertEqual(self.state()['group_size'],4)
        # Even after many confirmations, authoritative facts and the question survive history truncation.
        for _ in range(15): self.input('Yes')
        self.assertIn('hotel accommodations',str(build_messages(self.group,'')))
        self.assertTrue(self.save()['ok'])
        self.assertEqual(self.state()['budget_per_person_cents'],100000)

    def test_numeric_answers_follow_the_question_and_never_select_an_option(self):
        self.ask('How many people are traveling, including yourself?')
        self.input('4')
        model=ScriptedLLM(reply('Got it. Do you prefer a hotel or an apartment?'))
        out=Orchestrator(self.group.id,llm_chat=model).run(self.actor,text='4')
        self.assertEqual(len(model.requests),1)
        self.assertNotIn('shortlist',out[0].text)
        self.assertEqual(self.state()['group_size'],4)
        self.ask('What budget should I use per buyer?')
        self.input('300')
        self.assertEqual(self.state()['budget_per_person_cents'],30000)
        self.assertTrue(self.save(budget_per_person=300)['ok'])
        self.assertNotEqual(question_type('How many hotel rooms do you need?'),'count')

    def test_duplicate_budget_confirmation_is_repaired_into_tool_execution(self):
        self.ask('Do you also need hotels?')
        self.input('Yes')
        model=ScriptedLLM(reply('Could you confirm the $1,000 per person budget?'),
            reply(calls=[tool_call('save_buying_brief',{'goal':'China trip','category':'travel','group_size':4,'research_kinds':['flight','stay'],'open_questions':['Do you prefer one room or two?']})]),
            reply('Would you prefer one room or two?'))
        out=Orchestrator(self.group.id,llm_chat=model).run(self.actor,text='Yes')
        self.assertNotIn('confirm',out[0].text.lower())
        self.assertEqual(self.state()['budget_per_person_cents'],100000)

    @patch.dict('os.environ',{'TAVILY_API_KEY':'test-key'})
    def test_flight_route_dates_and_price_honesty(self):
        fake=FakeTavily(rows=[
            {'title':'$421 Flights from Guangzhou (CAN) to Seattle (SEA)','url':'https://example.com/routes/can/sea','content':'From $421'},
            {'title':'Cheap flights Seattle to Guangzhou','url':'https://example.com/routes/sea/can','content':'Advertised fares from $900'},
            {'title':'A hotel in China','url':'https://example.com/hotel','content':'Rooms from $20'},
        ])
        args={'kind':'flight','query':'Flights for our trip','group_size':4,'budget_per_person_max':1000,
              'route':{'origin':'Seattle','destination':'Guangzhou','origin_airport':'SEA','destination_airport':'CAN','trip_type':'round_trip'},
              'dates':{'start':'2026-10-20','end':'2026-10-27'}}
        with patch('purchases.live.httpx',fake):
            result=self.engine.execute('live_search',args,self.actor)
        self.assertTrue(result['ok'],result)
        query=fake.calls[0]['query']
        for value in ('Seattle','Guangzhou','2026-10-20','2026-10-27','round trip'):
            self.assertIn(value,query)
        offers=result['data']['offers']
        self.assertEqual(len(offers),1)
        self.assertEqual(offers[0]['url'],'https://example.com/routes/sea/can')
        self.assertIsNone(offers[0]['est_per_person_cents'])
        self.assertIsNone(offers[0]['fits_budget'])
        self.assertIn('not verified',offers[0]['price_note'])
        self.assertIsNone(live.per_person_estimate(42100,'listed',4,None,'flight'))
        self.assertEqual(live.per_person_estimate(42100,'person',4,None,'flight'),42100)
        self.assertFalse(self.engine.execute('live_search',{k:v for k,v in args.items() if k!='route'},self.actor)['ok'])

    def test_private_question_and_answer_never_reach_public_plan(self):
        self.ask('What hotel style do you prefer?')
        self.input('Quiet, with private rooms')
        self.assertTrue(self.save(recommendations=['Quiet hotel: private rooms, less shared space.','Apartment: shared kitchen and common room.'])['ok'])
        result=self.engine.execute('publish_buying_plan',{},self.actor)
        self.assertTrue(result['ok'],result)
        plan=TripPlan.objects.get(pk=result['data']['plan_id'])
        self.assertNotIn('_dialogue',plan.details)
        self.assertNotIn('What hotel style',str(plan.details))

    def test_question_context_exists_even_before_the_first_brief(self):
        self.session.planning_state={}
        self.session.save()
        self.ask('What airport will you leave from?')
        msg=self.input('Seattle Tacoma Airport')
        with patch('agent.llm.chat',side_effect=ProviderUnavailable(120)):
            services.run_agent(str(self.group.id),str(self.member.id),message_id=str(msg.id))
        self.assertIn('What airport',self.state()['_dialogue']['question'])
        self.assertEqual(self.state()['_dialogue']['answer'],'Seattle Tacoma Airport')
        self.assertEqual(build_group_state(self.group)['current_buying_brief'],{})

    def test_new_purchase_clears_old_trip_facts_but_short_confirmation_cannot(self):
        self.ask('Do you need hotels?')
        self.input('Yes')
        self.assertFalse(self.save(new_goal=True)['ok'])
        self.input('New goal: a birthday gift for myself, $50')
        result=self.engine.execute('save_buying_brief',{'new_goal':True,'goal':'Birthday gift','category':'gift','group_size':1,'budget_per_person':50},self.actor)
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.state()['group_size'],1)
        self.assertEqual(self.state()['budget_per_person_cents'],5000)
        self.assertNotIn('_dialogue',self.state())

    def test_old_flight_snapshots_do_not_keep_the_bad_group_split(self):
        offer=live.sanitize_offer({'kind':'flight','price_cents':42100,'price_unit':'listed','est_per_person_cents':10525,'fits_budget':True})
        self.assertIsNone(offer['est_per_person_cents'])
        self.assertIsNone(offer['fits_budget'])

    def test_relational_headcount_question_keeps_numeric_answer_through_outage(self):
        self.assertEqual(question_type('How many of you are going on the trip?'),'count')
        self.ask('How many of you are going on the trip?')
        self.input('5')
        self.assertEqual(self.state()['group_size'],5)
        self.assertTrue(self.state()['_buyer_count_confirmed'])
        self.input('6 people')
        self.assertEqual(self.state()['group_size'],6)
