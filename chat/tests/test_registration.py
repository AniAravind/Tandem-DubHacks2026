import hashlib
import hmac
import json
import os
import time
from datetime import timedelta
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from django.core import signing
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from chat.adapters.base import AgentMessage
from chat.models import ChatMessage, PhotonRegistration, PhotonSession
from chat.registration import (SALT, decrypt_phone, ensure_user, provision, registration_key, ticket_for)
from chat.services import post_card

PHONE = "+12065550123"
OTHER = "+12065550124"
DESTINATION = "+12065550999"


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "registration"}})
@patch.dict(os.environ, {"PHOTON_PUBLIC_REGISTRATION": "1", "SPECTRUM_PROJECT_ID": "test-project",
    "SPECTRUM_PROJECT_SECRET": "project-secret", "PHOTON_BRIDGE_SECRET": "bridge-secret",
    "PHOTON_TEST_PHONE": "+14257377163", "FIELD_ENCRYPTION_KEY": Fernet.generate_key().decode(), "CHAT_ADAPTER": "web"})
class RegistrationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def signup(self, phone=PHONE):
        return self.client.post('/api/v1/demo/registrations', {"phone": phone}, format="json")

    def status(self, token):
        return self.client.get('/api/v1/demo/registrations/status', HTTP_X_TANDEM_ONBOARDING=token)

    def row(self, phone=PHONE, allocation="shared"):
        with patch('chat.registration.enqueue'):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.signup(phone)
        self.assertEqual(response.status_code, 202, response.data)
        row = PhotonRegistration.objects.get(pk=registration_key(phone))
        row.destination = DESTINATION
        row.allocation = allocation
        row.status = "connecting"
        row.save()
        return row

    def bridge(self, sender=PHONE, route="shared", **data):
        body = json.dumps({"sender": sender, "route": route, **data})
        stamp = str(int(time.time()))
        signature = hmac.new(b"bridge-secret", f"{stamp}.{body}".encode(), hashlib.sha256).hexdigest()
        return self.client.post('/api/v1/chat/photon', body, content_type="application/json",
            HTTP_X_PHOTON_TIMESTAMP=stamp, HTTP_X_PHOTON_SIGNATURE=signature)

    def test_enrollment_is_encrypted_idempotent_and_sends_no_messages(self):
        with patch('chat.registration.enqueue') as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                first = self.signup('(206) 555-0123')
                second = self.signup(PHONE)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        enqueue.assert_called_once_with(registration_key(PHONE))
        row = PhotonRegistration.objects.get()
        self.assertNotIn(PHONE, row.phone_encrypted)
        self.assertEqual(decrypt_phone(row), PHONE)
        self.assertNotIn(PHONE, json.dumps(first.data))
        self.assertFalse(ChatMessage.objects.exists())
        self.assertFalse(PhotonSession.objects.exists())

    def test_disabled_invalid_phone_and_missing_encryption_do_not_provision(self):
        with patch.dict(os.environ, {"PHOTON_PUBLIC_REGISTRATION": "0"}):
            self.assertEqual(self.signup().status_code, 503)
        self.assertEqual(self.signup('not a number').status_code, 400)
        with patch.dict(os.environ, {"FIELD_ENCRYPTION_KEY": ""}):
            self.assertEqual(self.signup().status_code, 503)
        self.assertFalse(PhotonRegistration.objects.exists())

    def test_public_registration_is_throttled_per_phone(self):
        with patch('chat.registration.enqueue'):
            for _ in range(5):
                self.assertEqual(self.signup().status_code, 202)
            self.assertEqual(self.signup().status_code, 429)

    def test_status_needs_unexpired_separate_ticket_and_no_private_identity(self):
        row = self.row()
        token = ticket_for(row)
        self.assertEqual(self.status(token).data, {"status": "connecting"})
        self.assertEqual(self.status(token + 'bad').status_code, 410)
        self.assertEqual(self.status(signing.dumps({"registration": row.pk}, salt='tandem.member-session')).status_code, 410)
        with patch('django.core.signing.time.time', return_value=time.time() + 1801):
            self.assertEqual(self.status(token).status_code, 410)
        self.assertNotIn('member_id', self.status(token).data)

    def test_ready_only_after_route_heartbeat_and_becomes_connecting_if_stale(self):
        row = self.row()
        self.assertEqual(self.bridge(action='connected', route=DESTINATION).status_code, 403)
        self.assertEqual(self.bridge(action='connected').status_code, 200)
        response = self.status(ticket_for(row))
        self.assertEqual(response.data, {"status": "ready", "destination": DESTINATION})
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertFalse(PhotonSession.objects.exists())
        PhotonRegistration.objects.filter(pk=row.pk).update(connected_at=timezone.now()-timedelta(seconds=61))
        self.assertEqual(self.status(ticket_for(row)).data, {"status": "connecting"})

    @patch('chat.services.schedule_agent')
    def test_two_senders_have_separate_chats_private_outboxes_and_receipts(self, schedule):
        self.row()
        self.row(OTHER)
        for phone in (PHONE, OTHER):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.bridge(sender=phone, action='receive', text='Gift ideas', message_id='same-provider-id').status_code, 200)
        self.assertEqual(PhotonSession.objects.count(), 2)
        self.assertEqual(schedule.call_count, 2)
        first = PhotonSession.objects.get(pk=registration_key(PHONE))
        second = PhotonSession.objects.get(pk=registration_key(OTHER))
        self.assertNotEqual(first.member.group_id, second.member.group_id)
        private = post_card(first.member.group_id, AgentMessage('Private plan', private_to_member_id=str(first.member_id)))
        own = self.bridge(action='poll').data['messages']
        self.assertEqual([row['id'] for row in own], [private])
        self.assertEqual(self.bridge(sender=OTHER, action='poll').data['messages'], [])
        self.assertEqual(self.bridge(sender=OTHER, action='ack', seq=own[0]['seq']).status_code, 400)
        self.assertEqual(self.bridge(sender='+12065550125', action='receive', text='hello', message_id='x').status_code, 403)
        self.assertEqual(self.bridge(action='ack', seq=own[0]['seq']).status_code, 200)
        self.assertEqual(self.bridge(action='poll').data['messages'], [])

    def test_dedicated_route_requires_exact_assigned_line(self):
        self.row(allocation='dedicated')
        self.assertEqual(self.bridge(action='connected').status_code, 403)
        self.assertEqual(self.bridge(action='connected', route=DESTINATION).status_code, 200)

    def test_registry_is_signed_and_contains_no_onboarding_ticket(self):
        self.row()
        self.assertEqual(self.client.post('/api/v1/chat/photon', {'action': 'registry'}, format='json').status_code, 401)
        response = self.bridge(action='registry')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['routes'][0], {'sender': PHONE, 'destination': DESTINATION, 'allocation': 'shared'})
        self.assertNotIn('token', json.dumps(response.data))

    @patch('chat.registration.ensure_user')
    def test_provision_reuses_provider_user_then_waits_for_worker(self, ensure):
        row = self.row()
        row.status = 'queued'
        row.save()
        ensure.return_value = {'id': 'user-id', 'assignedPhoneNumber': DESTINATION, 'type': 'shared'}
        provision(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, 'connecting')
        self.assertEqual(row.provider_user_id, 'user-id')
        provision(row.pk)
        ensure.assert_called_once_with(PHONE)

    @patch('chat.registration.httpx.request')
    def test_provider_wire_contract_reuses_matching_user_and_handles_capacity(self, request):
        user = {'id': 'u1', 'phoneNumber': PHONE, 'assignedPhoneNumber': DESTINATION, 'type': 'shared'}
        def response(data, code=200):
            return httpx.Response(code, json=data, request=httpx.Request('GET', 'https://spectrum.photon.codes'))
        request.return_value = response({'succeed': True, 'data': {'users': [user], 'total': 1}})
        self.assertEqual(ensure_user(PHONE), user)
        self.assertEqual(request.call_count, 1)
        request.reset_mock()
        request.side_effect = [response({'succeed': True, 'data': {'users': [], 'total': 0}}),
                               response({'succeed': True, 'data': user})]
        self.assertEqual(ensure_user(PHONE), user)
        self.assertEqual(request.call_args.kwargs['json'], {'type': 'shared', 'phoneNumber': PHONE, 'sendInvite': False})
        self.assertFalse(request.call_args.kwargs['follow_redirects'])
        row = self.row()
        row.status = 'queued'
        row.save()
        request.side_effect = [response({'succeed': True, 'data': {'users': [], 'total': 0}}), response({}, 402)]
        provision(row.pk)
        row.refresh_from_db()
        self.assertEqual((row.status, row.error_code), ('failed', 'capacity'))
        self.assertNotIn(PHONE, json.dumps(self.status(ticket_for(row)).data))
