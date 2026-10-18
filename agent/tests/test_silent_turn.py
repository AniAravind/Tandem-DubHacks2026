from unittest.mock import patch
from django.test import TestCase
from agent.orchestrator import Orchestrator
from agent.tests.helpers import ScriptedLLM, reply, tool_call
from chat import services
from chat.adapters.base import InboundMessage
from chat.models import ChatMessage, PhotonSession
from core.models import Group, Member

class SilentTurnTests(TestCase):
    def planner(self, key):
        group=Group.objects.create(name=key,channel='imessage')
        member=Member.objects.create(group=group,display_name=key,role='organizer')
        group.organizer=member
        group.save()
        PhotonSession.objects.create(key=key,member=member)
        return group,member

    def test_empty_response_in_one_chat_does_not_silence_it_or_affect_other_chat(self):
        a,ma=self.planner('first')
        b,mb=self.planner('second')
        ia=services.store_inbound(InboundMessage(str(a.id),str(ma.id),'Continue','imessage'))
        ib=services.store_inbound(InboundMessage(str(b.id),str(mb.id),'Hello','imessage'))
        with patch('agent.llm.chat',ScriptedLLM(reply(''))):
            services.run_agent(str(a.id),str(ma.id),message_id=str(ia.id))
        with patch('agent.llm.chat',ScriptedLLM(reply('Hey! What are we planning?'))):
            services.run_agent(str(b.id),str(mb.id),message_id=str(ib.id))
        self.assertTrue(ChatMessage.objects.filter(group=a,direction='out').exclude(text_redacted='').exists())
        self.assertEqual(ChatMessage.objects.get(group=b,direction='out').text_redacted,'Hey! What are we planning?')

    def test_repeated_validation_failures_stop_and_reply_even_if_model_finishes_empty(self):
        group,member=self.planner('invalid')
        services.store_inbound(InboundMessage(str(group.id),str(member.id),'Continue','imessage'))
        bad=reply(calls=[tool_call('save_buying_brief',{'goal':'Trip','category':'travel','group_size':4,'budget_total':4000})])
        model=ScriptedLLM(bad,bad,reply(''))
        o=Orchestrator(group.id,llm_chat=model)
        out=o.run(f'member:{member.id}',text='Continue')
        self.assertEqual(len(model.requests),3)
        self.assertTrue(out[0].text)
        self.assertIn('budget',out[0].text)
