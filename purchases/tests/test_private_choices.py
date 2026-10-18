from unittest.mock import Mock, patch
from django.test import TestCase
from agent.orchestrator import Orchestrator
from chat.models import PhotonSession, ChatMessage
from purchases.models import TripPlan
from purchases.tests import test_plans


class PrivateChoiceTests(TestCase):
    setUp = test_plans.SharedPlanTests.setUp

    def select(self,text):
        model=Mock(side_effect=AssertionError('A saved choice must not call the model'))
        out=Orchestrator(self.source.id,engine=self.engine,llm_chat=model).run(self.actor,text=text)
        model.assert_not_called()
        return out

    def test_number_publishes_exact_second_option_once_and_resends_private_link(self):
        from purchases.serializers import latest_options_json
        second=latest_options_json(self.source)['options'][1]
        for _ in range(2):
            out=self.select('2')
            self.assertEqual(len(out),1)
            self.assertIn(second['title'],out[0].text)
            self.assertIn('?owner=',out[0].link)
            self.assertEqual(out[0].private_to_member_id,str(self.owner.id))
        plans=TripPlan.objects.filter(source_group=self.source,selected_option__meta__source_option_id=second['id'])
        self.assertEqual(plans.count(),1)
        self.assertFalse(self.source.purchases.exists())

    def test_exact_title_and_choose_phrase_are_deterministic(self):
        out=self.select('Choose '+self.option['title'])
        self.assertIn(str(self.plan.id),out[0].link)
        self.assertIn(str(self.plan.id),self.select('1')[0].link)

    def test_invalid_number_does_not_relist_or_pick_silently(self):
        out=self.select('9')
        self.assertIn('isn’t on this shortlist',out[0].text)
        self.assertFalse(out[0].link)

    def test_goal_change_cannot_choose_stale_quotes(self):
        session=PhotonSession.objects.get(member=self.owner)
        session.planning_state={'goal':'New gift','category':'gift_bundle','group_size':4,'revision':'new'}
        session.save()
        out=self.select('1')
        self.assertIn('don’t have current options',out[0].text)
        self.assertFalse(out[0].link)

    def test_saved_shortlist_refresh_is_free_of_model_and_duplicate_tables(self):
        session=PhotonSession.objects.get(member=self.owner)
        session.planning_state={'goal':'Tahoe','category':'rental','group_size':4,'revision':None}
        session.save()
        out=self.select('find options')
        self.assertIn('🔎',out[0].text)
        self.assertNotIn('|',str([m.text for m in out]))
        self.assertEqual(sum(bool(m.card) for m in out),3)

    @patch('chat.services.schedule_agent')
    def test_ingestion_choice_does_not_queue_a_model_turn(self,schedule):
        from chat.services import handle_vote_reply
        self.assertTrue(handle_vote_reply(str(self.source.id),str(self.owner.id),'1'))
        schedule.assert_not_called()
        msg=ChatMessage.objects.filter(group=self.source,direction='out').latest('seq')
        self.assertIn('?owner=',msg.text_redacted)
        self.assertEqual(msg.member_id,self.owner.id)

    @patch('agent.orchestrator.run')
    def test_obsolete_queued_turn_cannot_bury_new_selection(self,run):
        from chat.services import run_agent
        old=ChatMessage.objects.create(group=self.source,member=self.owner,direction='in',seq=10,text_redacted='find options')
        ChatMessage.objects.create(group=self.source,member=self.owner,direction='in',seq=11,text_redacted='1')
        run_agent(str(self.source.id),str(self.owner.id),message_id=str(old.id))
        run.assert_not_called()

    def test_search_before_brief_save_keeps_only_this_turns_matches_selectable(self):
        import json
        from purchases.models import Option
        old_id=self.option['id']
        ChatMessage.objects.create(group=self.source,member=self.owner,direction='in',seq=10,text_redacted='Tahoe for 4 buyers, $300 each')
        o=Orchestrator(self.source.id,engine=self.engine)
        searched=o.run_tool('search_options',json.dumps({'category':'rental','group_size':4,'budget_per_person_max':300}),self.actor)
        self.assertTrue(searched['ok'],searched)
        saved=o.run_tool('save_buying_brief',json.dumps({'goal':'Tahoe','category':'rental','group_size':4,'budget_per_person':300}),self.actor)
        self.assertTrue(saved['ok'],saved)
        self.assertIsNone(Option.objects.get(pk=old_id).meta.get('brief_revision'))
        out=self.select('1')
        self.assertTrue(out[0].link)
        plan=TripPlan.objects.filter(source_group=self.source).latest('created_at')
        self.assertEqual(plan.selected_option.meta['source_option_id'],searched['data']['options'][0]['id'])

    def test_copy_only_brief_updates_keep_displayed_quotes_selectable(self):
        import json
        ChatMessage.objects.create(group=self.source,member=self.owner,direction='in',seq=10,text_redacted='Tahoe for 4 buyers, $300 each')
        o=Orchestrator(self.source.id,engine=self.engine)
        args={'goal':'Tahoe','category':'rental','group_size':4,'budget_per_person':300}
        saved=o.run_tool('save_buying_brief',json.dumps(args),self.actor)
        o.run_tool('search_options',json.dumps({'category':'rental','group_size':4,'budget_per_person_max':300}),self.actor)
        updated=o.run_tool('save_buying_brief',json.dumps({**args,'summary':'A warmer description.','recommendations':['Consider a quiet cabin.']}),self.actor)
        self.assertEqual(saved['data']['revision'],updated['data']['revision'])
        self.assertTrue(self.select('1')[0].link)

    @patch('chat.services.schedule_agent')
    def test_signed_photon_choice_and_receipt_retry_return_one_private_plan(self,schedule):
        import hashlib,hmac,json,os,time
        from rest_framework.test import APIClient
        from chat.models import PhotonReceipt
        env={'PHOTON_BRIDGE_SECRET':'bridge-test','PHOTON_TEST_PHONE':self.phone,'SPECTRUM_PROJECT_ID':'project-test','CHAT_ADAPTER':'web'}
        key=hashlib.sha256(('project-test'+self.phone).encode()).hexdigest()
        PhotonSession.objects.filter(member=self.owner).update(key=key)
        body=json.dumps({'sender':self.phone,'action':'receive','message_id':'choice-1','text':'1'})
        stamp=str(int(time.time()))
        signature=hmac.new(b'bridge-test',f'{stamp}.{body}'.encode(),hashlib.sha256).hexdigest()
        with patch.dict(os.environ,env),self.captureOnCommitCallbacks(execute=True):
            for _ in range(2):
                response=APIClient().post('/api/v1/chat/photon',body,content_type='application/json',HTTP_X_PHOTON_TIMESTAMP=stamp,HTTP_X_PHOTON_SIGNATURE=signature)
                self.assertEqual(response.status_code,200,response.json())
                self.assertEqual(len(response.json()['messages']),1)
                self.assertIn('?owner=',response.json()['messages'][0]['text'])
        schedule.assert_not_called()
        self.assertEqual(PhotonReceipt.objects.filter(message__group=self.source).count(),1)
        self.assertEqual(ChatMessage.objects.filter(group=self.source,direction='out').count(),1)
