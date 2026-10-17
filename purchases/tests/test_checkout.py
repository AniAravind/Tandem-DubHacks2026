"""
Tests for checkout, capture and compensation (milestone M4).

The world: the six-person Tahoe purchase from test_rules_holds (Pinecrest A-Frame, $1,050.00, six $175.00 shares),
every card is good, and every member has already placed a hold, so the purchase is "funded" and ready to book.
The merchant is the in-process DirectMerchantClient, which verifies signatures like the real one will.
"""
import json
from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from chat.models import ChatMessage
from core.models import EventLog
from merchant import signing
from merchant.models import Listing, MerchantBooking, SignedRequest
from payments.gateway import GatewayError
from payments.models import Hold, Mandate
from payments.simulated import SimulatedVisaAgentGateway
from purchases import rules
from purchases.merchant_client import DirectMerchantClient
from purchases.models import Booking, Purchase, Refund, Share, Vote
from purchases.tests.test_rules_holds import approve, build_world


class CountingMerchant:
    """Wraps the direct merchant, counts calls per action, and can force an action to fail."""

    def __init__(self, fail=None):
        self.inner, self.calls, self.fail = DirectMerchantClient(), [], fail or {}

    def send(self, slug, action, headers, body):
        self.calls.append(action)
        if action in self.fail:
            return self.fail[action]
        return self.inner.send(slug, action, headers, body)


import base64
import os

# A throwaway key used only by these tests, so they pass no matter what is (or is not) in anyone's .env file.
TEST_SIGNING_KEY_B64 = base64.b64encode(bytes(range(32))).decode()


class CheckoutBase(TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"AGENT_SIGNING_KEY_B64": TEST_SIGNING_KEY_B64})
        env.start()
        self.addCleanup(env.stop)
        cache.clear()                                         # forget used nonces and demo switches
        self.gateway = SimulatedVisaAgentGateway(latency_ms=0)
        self.merchant = CountingMerchant()
        for target, value in (("purchases.rules.get_gateway", self.gateway),
                              ("purchases.rules.get_merchant_client", self.merchant)):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.purchase, self.parts = build_world(self.gateway, all_cards_ok=True)

    def fund(self):
        """Everybody places a hold. The purchase becomes funded; booking is NOT run (on_commit is not executed here)."""
        for name in self.parts:
            approve(name, self.parts)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "funded")

    def events(self, type):
        return list(EventLog.objects.filter(type=type))


class HappyPathTests(CheckoutBase):
    def test_booking_happens_automatically_after_the_last_hold(self):
        for name in list(self.parts)[:5]:
            approve(name, self.parts)
        with self.captureOnCommitCallbacks(execute=True):
            approve(list(self.parts)[5], self.parts)           # the sixth hold funds the purchase, then it books
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "booked")

    def test_checkout_books_and_captures_everyone(self):
        self.fund()
        result = rules.checkout(self.purchase.id)
        self.assertEqual(result["status"], "booked")
        self.assertEqual(result["captured"], 6)
        self.purchase.refresh_from_db()
        self.assertEqual((self.purchase.state, self.purchase.booked_at is not None), ("booked", True))
        booking = Booking.objects.get(purchase=self.purchase)
        self.assertEqual((booking.amount_cents, booking.status), (105000, "confirmed"))
        self.assertTrue(MerchantBooking.objects.filter(booking_ref=booking.merchant_booking_ref).exists())
        self.assertTrue(all(s.status == "captured" for s in Share.objects.filter(purchase=self.purchase)))
        self.assertEqual(Hold.objects.filter(status="captured").count(), 6)
        self.assertEqual(len(self.gateway.captures), 6)
        self.assertEqual(sum(c["amount"] for c in self.gateway.captures.values()), 105000)
        self.assertEqual(Mandate.objects.filter(status="consumed").count(), 6)

    def test_signed_requests_are_audited(self):
        self.fund()
        rules.checkout(self.purchase.id)
        quote, checkout = SignedRequest.objects.order_by("created_at")
        self.assertEqual((quote.purpose, quote.tag, quote.response_code), ("browse", "agent-browser-auth", 200))
        self.assertEqual((checkout.purpose, checkout.tag, checkout.response_code, checkout.verified),
                         ("checkout", "agent-payer-auth", 201, True))
        self.assertTrue(checkout.content_digest.startswith("sha-256=:"))
        self.assertEqual(Booking.objects.get().signed_request_id, checkout.id)
        self.assertEqual(len(self.events("signer.request_signed")), 2)
        self.assertEqual(len(self.events("merchant.request_accepted")), 1)
        self.assertEqual(len(self.events("booking.confirmed")), 1)

    def test_idempotent_capture(self):
        self.fund()
        rules.checkout(self.purchase.id)
        again = rules.checkout(self.purchase.id)                # a second call must not book or charge again
        self.assertEqual(again["status"], "already_booked")
        self.assertEqual(len(self.gateway.captures), 6)
        self.assertEqual(MerchantBooking.objects.count(), 1)
        self.assertEqual(self.merchant.calls.count("agent/checkout"), 1)
        # and the gateway itself returns the same capture for the same key
        share = Share.objects.filter(purchase=self.purchase).first()
        hold = share.holds.get(status="captured")
        key = rules._idem_key(share.id, "capture", self.purchase.version)
        repeat = self.gateway.capture(hold.gateway_hold_id, rules.Money(hold.amount_cents), key)
        self.assertEqual(repeat["capture_id"], hold.gateway_capture_id)
        self.assertEqual(len(self.gateway.captures), 6)

    def test_confirmation_card_is_posted_with_dates(self):
        option = self.purchase.option
        option.meta = {"check_in": "2026-11-14", "check_out": "2026-11-16", "cancellation": "Free until Nov 7"}
        option.save()
        self.fund()
        rules.checkout(self.purchase.id)
        card = ChatMessage.objects.filter(card__type="BookingConfirmed").get().card["data"]
        self.assertEqual(card["dates"], "Nov 14 to 16")
        self.assertEqual((card["title"], card["per_person_cents"], card["signature_verified"], card["simulated"]),
                         ("Pinecrest A-Frame", 17500, True, True))
        self.assertEqual(card["booking_ref"], Booking.objects.get().merchant_booking_ref)
        self.assertEqual(Booking.objects.get().cancellation_policy, {"text": "Free until Nov 7"})

    def test_dates_label(self):
        self.assertEqual(rules._dates_label("2026-11-14", "2026-11-16"), "Nov 14 to 16")
        self.assertEqual(rules._dates_label("2026-11-30", "2026-12-02"), "Nov 30 to Dec 2")
        self.assertEqual(rules._dates_label(None, None), "")


class RejectionTests(CheckoutBase):
    def test_checkout_rejected_no_capture(self):
        """The demo moment: the price is changed AFTER signing. The merchant refuses and nobody is charged."""
        self.fund()
        cache.set("demo:tamper_next", True)
        result = rules.checkout(self.purchase.id)
        self.assertEqual(result, {"status": "rejected", "reason": "digest_mismatch"})
        self.assertEqual(self.gateway.captures, {})
        self.assertEqual(MerchantBooking.objects.count(), 0)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "funded")                        # still funded, holds kept
        self.assertTrue(all(s.status == "held" for s in Share.objects.filter(purchase=self.purchase)))
        row = SignedRequest.objects.get(purpose="checkout")
        self.assertEqual((row.response_code, row.verified, row.reject_reason), (401, False, "digest_mismatch"))
        rejected = self.events("merchant.request_rejected")
        self.assertEqual([e.payload for e in rejected], [{"reason": "digest_mismatch", "status": 401}])
        self.assertEqual(self.merchant.calls.count("agent/checkout"), 1)       # a 401 is NOT retried

    def test_after_a_rejection_a_fresh_checkout_succeeds(self):
        self.fund()
        cache.set("demo:tamper_next", True)
        rules.checkout(self.purchase.id)
        self.assertEqual(rules.checkout(self.purchase.id)["status"], "booked")  # the switch was one-shot
        self.assertEqual(len(self.gateway.captures), 6)

    def test_merchant_lock(self):
        self.fund()
        Mandate.objects.filter(share=self.parts["Maya"][0]).update(listing_ref="juniper-loft")
        result = rules.checkout(self.purchase.id)
        self.assertEqual(result, {"status": "rejected", "reason": "merchant_lock"})
        self.assertEqual(self.merchant.calls, [])                              # never even asked the merchant
        self.assertEqual(self.gateway.captures, {})

    def test_expired_deadline_blocks_checkout(self):
        from datetime import timedelta
        from django.utils import timezone
        self.fund()
        Purchase.objects.filter(pk=self.purchase.pk).update(deadline_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(rules.checkout(self.purchase.id), {"status": "rejected", "reason": "expired"})
        self.assertEqual(self.gateway.captures, {})

    def test_not_funded_cannot_check_out(self):
        approve("Maya", self.parts)
        self.assertEqual(rules.checkout(self.purchase.id), {"status": "not_funded"})
        self.assertEqual(self.merchant.calls, [])

    def test_price_change_keeps_holds_and_opens_a_vote(self):
        self.fund()
        Listing.objects.filter(ref="pinecrest-a-frame").update(total_cents=114000)
        result = rules.checkout(self.purchase.id)
        self.assertEqual(result, {"status": "price_changed", "quoted_cents": 114000, "expected_cents": 105000})
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "collecting")
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 6)  # holds kept
        self.assertEqual(self.gateway.captures, {})
        vote = Vote.objects.get(purpose="price_change")
        self.assertEqual((vote.status, vote.purchase_id, vote.quorum), ("open", self.purchase.id, 4))
        card = ChatMessage.objects.filter(card__type="VoteCard").get()
        self.assertIn("$1,140.00", card.text_redacted)
        self.assertEqual(self.merchant.calls.count("agent/checkout"), 0)

    def test_server_errors_are_retried_exactly_once(self):
        self.fund()
        self.merchant.fail = {"agent/checkout": (503, {"error": {"code": "merchant_down", "message": "try later"}})}
        result = rules.checkout(self.purchase.id)
        self.assertEqual(result, {"status": "rejected", "reason": "merchant_down"})
        self.assertEqual(self.merchant.calls.count("agent/checkout"), 2)       # first try + one retry
        self.assertEqual(self.gateway.captures, {})
        self.assertEqual(SignedRequest.objects.filter(purpose="checkout").count(), 2)    # each try signed freshly

    def test_quote_failure_stops_before_checkout(self):
        self.fund()
        self.merchant.fail = {"quotes": (404, {"error": {"code": "listing_not_found", "message": "gone"}})}
        self.assertEqual(rules.checkout(self.purchase.id), {"status": "rejected", "reason": "listing_not_found"})
        self.assertNotIn("agent/checkout", self.merchant.calls)


class CompensationTests(CheckoutBase):
    def test_compensation_refunds(self):
        """R13: the merchant booked, but the 4th capture fails. Cancel the booking and give everybody their money back."""
        self.fund()
        real_capture, calls = self.gateway.capture, []

        def flaky(hold_id, amount, key):
            calls.append(hold_id)
            if len(calls) == 4:
                raise GatewayError("capture_failed", "simulated network failure", 502)
            return real_capture(hold_id, amount, key)

        with mock.patch.object(self.gateway, "capture", side_effect=flaky):
            result = rules.checkout(self.purchase.id)
        self.assertEqual(result, {"status": "compensated", "reason": "capture_failed"})
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "refunded")
        booking = Booking.objects.get()
        self.assertEqual(booking.status, "cancelled")
        self.assertEqual(MerchantBooking.objects.get().status, "cancelled")        # signed cancel reached the merchant
        statuses = sorted(s.status for s in Share.objects.filter(purchase=self.purchase))
        self.assertEqual(statuses, ["refunded"] * 3 + ["released"] * 3)
        self.assertEqual(Refund.objects.count(), 3)
        self.assertEqual(sum(r.amount_cents for r in Refund.objects.all()), 3 * 17500)
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 0)      # nothing left on hold
        self.assertEqual(sum(c["refunded"] for c in self.gateway.captures.values()), 3 * 17500)
        self.assertTrue(all(h["status"] in ("CAPTURED", "RELEASED") for h in self.gateway.holds.values()))
        self.assertEqual(len(self.events("booking.cancelled")), 1)
        moves = [(e.payload["from"], e.payload["to"]) for e in self.events("purchase.state_changed")][-4:]
        self.assertEqual(moves, [("collecting", "funded"), ("funded", "booked"), ("booked", "cancelled"), ("cancelled", "refunded")])

    def test_cancel_request_is_signed_with_the_payer_tag(self):
        self.fund()
        with mock.patch.object(self.gateway, "capture", side_effect=GatewayError("capture_failed", "x")):
            rules.checkout(self.purchase.id)                 # the very first capture fails, so we cancel
        cancel = SignedRequest.objects.get(purpose="cancel")
        self.assertEqual((cancel.tag, cancel.response_code), ("agent-payer-auth", 200))


class DirectMerchantVerifierTests(CheckoutBase):
    """The stand-in merchant must reject exactly what the real one will (section 6.9 table)."""

    def sign(self, body: dict, tag="agent-payer-auth", action="agent/checkout"):
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        path = f"/merchants/pinestay/{action}"
        return raw, signing.sign_request("localhost:8000", path, raw, tag)

    def test_good_request_passes_the_signature_checks(self):
        raw, headers = self.sign({"quote_id": "nope", "listing_ref": "pinecrest-a-frame", "amount_cents": 1})
        status, data = DirectMerchantClient().send("pinestay", "agent/checkout", headers, raw)
        self.assertEqual((status, data["error"]["code"]), (409, "quote_mismatch"))     # got past 1 to 7, failed check 8

    def test_changed_body_is_digest_mismatch(self):
        raw, headers = self.sign({"amount_cents": 105000})
        status, data = DirectMerchantClient().send("pinestay", "agent/checkout", headers, raw.replace(b"105000", b"95000"))
        self.assertEqual((status, data["error"]["code"]), (401, "digest_mismatch"))

    def test_replayed_nonce(self):
        raw, headers = self.sign({"amount_cents": 1})
        client = DirectMerchantClient()
        client.send("pinestay", "agent/checkout", headers, raw)
        status, data = client.send("pinestay", "agent/checkout", headers, raw)
        self.assertEqual((status, data["error"]["code"]), (401, "nonce_replayed"))

    def test_wrong_tag_and_missing_signature(self):
        raw, headers = self.sign({"amount_cents": 1}, tag="agent-browser-auth")
        self.assertEqual(DirectMerchantClient().send("pinestay", "agent/checkout", headers, raw)[1]["error"]["code"], "wrong_tag")
        self.assertEqual(DirectMerchantClient().send("pinestay", "agent/checkout", {}, raw)[1]["error"]["code"], "signature_missing")

    def test_forged_signature(self):
        raw, headers = self.sign({"amount_cents": 1})
        headers["Signature"] = "sig2=:" + "QUJD" * 16 + ":"
        status, data = DirectMerchantClient().send("pinestay", "agent/checkout", headers, raw)
        self.assertEqual((status, data["error"]["code"]), (401, "signature_invalid"))

    def test_different_path_breaks_the_signature(self):
        raw, headers = self.sign({"amount_cents": 1})                    # signed for checkout...
        status, data = DirectMerchantClient().send("pinestay", "agent/cancel", headers, raw)   # ...sent to cancel
        self.assertEqual((status, data["error"]["code"]), (401, "signature_invalid"))
