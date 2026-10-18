"""Authenticated SDK bridge, isolated by registered sender and assigned route."""
import hashlib
import hmac
import os
import time

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from chat import services
from chat.adapters.base import InboundMessage
from chat.api import message_json
from chat.models import ChatMessage, PhotonReceipt, PhotonSession, PhotonRegistration
from core.models import Group, Member
from chat.registration import decrypt_phone, registration_key


@api_view(["POST"])
def bridge(request):
    secret = os.getenv("PHOTON_BRIDGE_SECRET", "")
    stamp = request.headers.get("X-Photon-Timestamp", "")
    expected = hmac.new(secret.encode(), stamp.encode() + b"." + request.body, hashlib.sha256).hexdigest()
    try:
        fresh = abs(time.time() - int(stamp)) <= 60
    except ValueError:
        fresh = False
    if not secret or not fresh or not hmac.compare_digest(expected, request.headers.get("X-Photon-Signature", "")):
        return Response({"error": "unauthorized"}, status=401)
    data = request.data
    if not isinstance(data, dict):
        return Response({"error": "invalid_request"}, status=400)
    if data.get("action") == "registry":
        routes = [{"sender": decrypt_phone(row), "destination": row.destination, "allocation": row.allocation}
                  for row in PhotonRegistration.objects.filter(project_id=os.getenv("SPECTRUM_PROJECT_ID", ""), status__in=["connecting", "ready", "active"])]
        legacy = os.getenv("PHOTON_TEST_PHONE", "")
        if legacy and not any(row["sender"] == legacy for row in routes):
            routes.append({"sender": legacy, "destination": None, "allocation": "legacy"})
        response = Response({"routes": routes})
        response["Cache-Control"] = "no-store"
        return response
    phone = os.getenv("PHOTON_TEST_PHONE", "")
    sender = data.get("sender")
    registration = PhotonRegistration.objects.filter(pk=registration_key(sender)).first() if isinstance(sender, str) else None
    if registration and registration.status not in {"connecting", "ready", "active"}:
        registration = None
    legacy = bool(phone and sender == phone)
    if not registration and not legacy:
        return Response({"error": "sender_not_authorized"}, status=403)
    phone = sender
    action = data.get("action")
    if action not in {"receive", "poll", "ack", "connected"}:
        return Response({"error": "invalid_action"}, status=400)
    if registration:
        expected_route = "shared" if registration.allocation == "shared" else registration.destination
        if data.get("route") != expected_route:
            return Response({"error": "route_not_authorized"}, status=403)
    if action == "connected":
        if registration:
            PhotonRegistration.objects.filter(pk=registration.pk).update(
                connected_at=timezone.now(), status="active" if registration.activated_at else "ready")
        return Response({"ok": True})
    if action == "receive" and (not isinstance(data.get("text"), str) or not data["text"].strip()
                                or len(data["text"]) > 2000 or not isinstance(data.get("message_id"), str)
                                or not data["message_id"] or len(data["message_id"]) > 512):
        return Response({"error": "invalid_message"}, status=400)
    key = hashlib.sha256((os.getenv("SPECTRUM_PROJECT_ID", "") + phone).encode()).hexdigest()
    if registration and not registration.activated_at and action != "receive" and not PhotonSession.objects.filter(pk=key).exists():
        return Response({"messages": []})
    with transaction.atomic():
        # Serialize creation and ingestion using a stable DB resource, across web processes.
        from django.db import connection
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [int(key[:15], 16)])
        session = PhotonSession.objects.select_for_update().filter(pk=key).first()
        if session is None:
            group = Group.objects.create(name="Tandem · personal agent", channel="imessage")
            member = Member.objects.create(group=group, display_name="Organizer", role="organizer",
                                          phone_e164_hash=hashlib.sha256(phone.encode()).hexdigest(), phone_last4=phone[-4:])
            group.organizer = member
            group.save(update_fields=["organizer"])
            session = PhotonSession.objects.create(key=key, member=member)
        member = session.member
        if action == "receive":
            if registration:
                PhotonRegistration.objects.filter(pk=registration.pk, activated_at=None).update(activated_at=timezone.now(), status="active")
            external_id = hashlib.sha256((key + data["message_id"]).encode()).hexdigest()
            if not PhotonReceipt.objects.filter(pk=external_id).exists():
                msg = InboundMessage(str(member.group_id), str(member.id), data["text"], "imessage", external_id)
                try:
                    stored = services.store_inbound(msg)
                except services.RateLimited:
                    return Response({"error": "rate_limited"}, status=429)
                PhotonReceipt.objects.create(external_id=external_id, message=stored)
                vote = services.VOTE_REPLY.match(msg.text)
                if not (vote and services.handle_vote_reply(msg.group_id, msg.member_id, vote.group(1))):
                    transaction.on_commit(lambda: services.schedule_agent(msg.group_id, msg.member_id, message_id=str(stored.id)))
        elif action == "ack":
            seq = data.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < session.delivered_seq:
                return Response({"error": "invalid_cursor"}, status=400)
            if not ChatMessage.objects.filter(group_id=member.group_id, seq=seq, direction="out").filter(Q(member=member) | Q(member=None)).exists():
                return Response({"error": "invalid_cursor"}, status=400)
            session.delivered_seq = seq
            session.save(update_fields=["delivered_seq"])
        rows = ChatMessage.objects.filter(group_id=member.group_id, direction="out", seq__gt=session.delivered_seq).filter(Q(member=member) | Q(member=None)).order_by("seq")[:50]
        return Response({"messages": [message_json(row) for row in rows], "group_id": str(member.group_id)})
