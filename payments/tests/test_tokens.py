from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.models import Member
from payments.services import ConsentError, make_consent_token, read_consent_token
from purchases.models import Option, Purchase, Share

SHARE_CENTS = 17_500
TOTAL_CENTS = SHARE_CENTS * 6


def make_share(**kwargs):
    call_command("seed_demo", stdout=StringIO())
    member = Member.objects.get(display_name="Jordan")
    option = Option.objects.create(
        group=member.group, merchant_slug="pinestay", listing_ref="pinecrest-a-frame",
        category="rental", title="Pinecrest A-Frame", total_cents=TOTAL_CENTS, capacity=6)
    purchase = Purchase.objects.create(
        group=member.group, option=option, state=Purchase.State.COLLECTING, total_cents=TOTAL_CENTS,
        deadline_at=timezone.now() + timedelta(days=3), **kwargs.pop("purchase_defaults", {}))
    share = Share.objects.create(
        purchase=purchase, member=member, amount_cents=SHARE_CENTS, status=Share.Status.INVITED,
        purchase_version=purchase.version)
    return share


class ConsentTokenTests(TestCase):
    def test_round_trip(self):
        share = make_share()
        token = make_consent_token(share)
        loaded = read_consent_token(token)["share"]
        self.assertEqual(loaded.id, share.id)

    def test_tampered_token(self):
        token = make_consent_token(make_share()) + "nope"
        with self.assertRaises(ConsentError) as ctx:
            read_consent_token(token)
        self.assertEqual(ctx.exception.status, 410)
        self.assertEqual(ctx.exception.code, "link_expired")

    def test_expired_deadline(self):
        share = make_share()
        share.purchase.deadline_at = timezone.now() - timedelta(minutes=1)
        share.purchase.save()
        token = make_consent_token(share)
        with self.assertRaises(ConsentError) as ctx:
            read_consent_token(token)
        self.assertEqual(ctx.exception.code, "link_expired")
        self.assertEqual(ctx.exception.status, 410)

    def test_stale_version(self):
        share = make_share()
        token = make_consent_token(share)
        share.purchase.version = 2
        share.purchase.save()
        with self.assertRaises(ConsentError) as ctx:
            read_consent_token(token)
        self.assertEqual(ctx.exception.code, "link_superseded")
        self.assertEqual(ctx.exception.status, 410)
