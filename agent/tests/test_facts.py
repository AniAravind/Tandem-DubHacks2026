"""Facts persist before planning; later planner calls cannot rewrite them."""
from unittest.mock import patch
from django.test import override_settings
from agent.tests import test_conversation_state
from django.test import TestCase
from agent.facts import capture,strict_schema,omit_unknown
from chat.models import PhotonSession


@override_settings(PRIVATE_LIVE_ONLY=True)
class FactCaptureTests(TestCase):
    setUp=test_conversation_state.ConversationStateTests.setUp
    input=test_conversation_state.ConversationStateTests.input
    save=test_conversation_state.ConversationStateTests.save
    state=test_conversation_state.ConversationStateTests.state
    ask=test_conversation_state.ConversationStateTests.ask
    def test_full_message_sequence_captures_facts_and_locks_them_against_planner(self):
        self.session.planning_state={}
        self.session.save()
        messages=[
            ('Hi',None),
            ('Trip to Hawaii with my friends',{'goal':'Hawaii trip','category':'travel','group_size':1}),
            ('3 friends and me. Whole package',{'group_size':4,'group_size_evidence':'3 friends and me','research_kinds':['flight','stay','activity']}),
            ('I wanna travel for thanksgiving break, like 25th to 30th',{'decision_details':[{'topic':'dates','value':'2026-11-25 to 2026-11-30'}]}),
            ('SeaTac. Hotel should be nice. Adventure themes',{'decision_details':[{'topic':'origin','value':'SEA'},{'topic':'activities','value':'adventure'}]}),
            ('A beachfront resort w a pool',{'requirements':['beachfront resort','pool']}),
            ('1000 per person',{'budget_per_person':1000,'group_size_evidence':'3 friends and me'}),
        ]
        for text,updates in messages:
            self.input(text)
            def reader(context,schema):
                if updates is None: return None
                args={'goal':'Hawaii trip','category':'travel','group_size':context['saved'].get('group_size',1),**updates}
                args['source_message_seq']=context['current_message']['seq']
                return args
            capture(self.group,self.actor,reader)
        state=self.state()
        self.assertEqual(state['group_size'],4)
        self.assertEqual(state['budget_per_person_cents'],100000)
        self.assertEqual(state['budget_total_cents'],400000)
        self.assertEqual(state['requirements'],['beachfront resort','pool'])
        self.assertIn({'topic':'dates','value':'2026-11-25 to 2026-11-30'},state['decision_details'])
        result=self.save(requirements=[],decision_details=[{'topic':'dates','value':'2026-10-25 to 2026-10-30'}])
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.state()['requirements'],['beachfront resort','pool'])
        self.assertIn({'topic':'dates','value':'2026-11-25 to 2026-11-30'},self.state()['decision_details'])

    def test_optional_nulls_are_omitted_but_required_nulls_still_fail_validation(self):
        from agent.validate import validate
        schema={'type':'object','required':['goal'],'properties':{'goal':{'type':'string'},'budget':{'type':'number'}}}
        compiled=strict_schema(schema)
        self.assertEqual(compiled['required'],['goal','budget'])
        self.assertIn({'type':'null'},compiled['properties']['budget']['anyOf'])
        self.assertEqual(omit_unknown({'goal':'Trip','budget':None},schema),{'goal':'Trip'})
        self.assertTrue(validate(omit_unknown({'goal':None,'budget':None},schema),schema))

    def test_capture_failure_preserves_existing_state_and_allows_planner_recovery(self):
        before=self.state()
        self.input('A beachfront resort')
        capture(self.group,self.actor,lambda *args:False)
        self.assertEqual(self.state()['goal'],before['goal'])
        self.assertNotIn('_verified_context',self.state())

    def test_older_fact_capture_cannot_rollback_a_newer_message(self):
        from purchases.briefs import save
        first=self.input('A beachfront resort')
        later=self.input('Actually a mountain cabin')
        args={'goal':'China trip','category':'travel','group_size':4,'requirements':['mountain cabin']}
        save(self.member,args,verified_facts=True,fact_seq=later.seq)
        with self.assertRaises(Exception) as caught:
            save(self.member,{**args,'requirements':['beachfront resort']},verified_facts=True,fact_seq=first.seq)
        self.assertEqual(caught.exception.code,'stale_fact_update')
        self.assertEqual(self.state()['requirements'],['mountain cabin'])

    def test_unconfirmed_budget_does_not_discard_dates_and_preferences(self):
        self.session.planning_state={}
        self.session.save()
        self.input('11/17-11/27. 1000. Hotel, whatever activities you think are right')
        capture(self.group,self.actor,lambda *args:{'goal':'Trip','category':'travel','group_size':4,
            'budget_total':4000,'requirements':['hotel'],
            'decision_details':[{'topic':'dates','value':'2026-11-17 to 2026-11-27'}]})
        state=self.state()
        self.assertIsNone(state['budget_per_person_cents'])
        self.assertEqual(state['requirements'],['hotel'])
        self.assertEqual(state['decision_details'],[{'topic':'dates','value':'2026-11-17 to 2026-11-27'}])
        self.assertIn('Is the budget per person or for everyone?',state['open_questions'])
