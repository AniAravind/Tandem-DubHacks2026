import hashlib
import hmac
import json
import os
import time
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from chat.adapters.base import AgentMessage
from chat.models import ChatMessage, PhotonReceipt, PhotonSession
from chat.services import post_card


@patch.dict(os.environ, {"PHOTON_BRIDGE_SECRET": "test-bridge-secret", "PHOTON_TEST_PHONE": "+14257377163", "SPECTRUM_PROJECT_ID": "test-project", "CHAT_ADAPTER": "web"})
class PhotonBridgeTests(TestCase):
    def call(self, data=None, stamp=None, secret="test-bridge-secret"):
        body = json.dumps({"sender": "+14257377163", **(data or {})})
        stamp = str(stamp if stamp is not None else int(time.time()))
        signature = hmac.new(secret.encode(), f"{stamp}.{body}".encode(), hashlib.sha256).hexdigest()
        return APIClient().post('/api/v1/chat/photon', body, content_type='application/json', HTTP_X_PHOTON_TIMESTAMP=stamp, HTTP_X_PHOTON_SIGNATURE=signature)

    def test_rejects_unsigned_expired_wrong_sender(self):
        self.assertEqual(APIClient().post('/api/v1/chat/photon', {}, format='json').status_code, 401)
        self.assertEqual(self.call({'action': 'poll'}, stamp=0).status_code, 401)
        self.assertEqual(self.call({'action': 'poll'}, secret='wrong').status_code, 401)
        self.assertEqual(self.call({'action': 'poll', 'sender': '+15555550199'}).status_code, 403)
        self.assertFalse(PhotonSession.objects.exists())

    @patch('chat.services.schedule_agent')
    def test_duplicate_inbound_runs_once_and_uses_original_message(self, schedule):
        data = {'action': 'receive', 'message_id': 'provider-id-1', 'text': 'find me a cabin'}
        with self.captureOnCommitCallbacks(execute=True):
            first = self.call(data)
            second = self.call(data)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(PhotonReceipt.objects.count(), 1)
        message = ChatMessage.objects.get(direction='in')
        self.assertEqual(message.channel, 'imessage')
        schedule.assert_called_once_with(str(message.group_id), str(message.member_id), message_id=str(message.id))
        self.assertEqual(message.group.members.count(), 1)

    def test_private_delivery_cursor_persists(self):
        self.call({'action': 'poll'})
        session = PhotonSession.objects.get()
        mid = post_card(session.member.group_id, AgentMessage('Your private link', private_to_member_id=str(session.member_id)))
        message = ChatMessage.objects.get(pk=mid)
        poll = self.call({'action': 'poll'}).json()
        self.assertEqual([row['id'] for row in poll['messages']], [mid])
        self.assertEqual(self.call({'action': 'ack', 'seq': 999}).status_code, 400)
        self.assertEqual(self.call({'action': 'ack', 'seq': message.seq}).status_code, 200)
        self.assertEqual(self.call({'action': 'poll'}).json()['messages'], [])
