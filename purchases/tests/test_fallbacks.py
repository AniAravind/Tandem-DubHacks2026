"""
Tests for fallbacks and share changes (milestone M6).

The story: six people, Pinecrest A-Frame at $1,050.00 ($175.00 each). Everyone but Jordan puts a hold down
(card limit approved: $190.00). Jordan taps "Not for me". The group needs a plan for five people.
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.events import log_event_simulated
from core.models import EventLog, Member
from payments.models import Consent, Hold, Mandate
from payments.simulated import SimulatedVisaAgentGateway
from purchases import consensus, rules
from purchases.models import Ballot, Option, Purchase, Share, Vote
from purchases.tests.test_rules_holds import CAP_CENTS, SHARE_CENTS, approve, build_world

S = Share.Status


class FallbackBase(TestCase):
    def setUp(self):
        self.gateway = SimulatedVisaAgentGateway(latency_ms=0, on_event=log_event_simulated)
        patcher = mock.patch("purchases.rules.get_gateway", return_value=self.gateway)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.purchase, self.parts = build_world(self.gateway, all_cards_ok=True)
        self.group = self.purchase.group
        self.members = {m.display_name: m for m in Member.objects.all()}
        self.engine = rules.RulesEngine()

    # ---- helpers
    def hold_everyone_but_jordan(self, cap=CAP_CENTS):
        for name in self.parts:
            if name != "Jordan":
                approve(name, self.parts, cap=cap)
        rules.member_withdraw(self.parts["Jordan"][0].id, "declined")

    def propose(self):
        result = self.engine.execute("propose_fallback", {"purchase_id": str(self.purchase.id), "reason": "decline"},
                                     f"member:{self.members['Maya'].id}")
        self.assertTrue(result["ok"], result)
        return result["data"]

    def vote_all(self, vote_id, option_id):
        """Everyone still in votes for the choice whose kind is `option_id`. Returns the closed Vote."""
        vote = Vote.objects.get(pk=vote_id)
        key = next(c["key"] for c in vote.choices if c["option_id"] == option_id)
        for name, member in self.members.items():
            if name != "Jordan":
                Ballot.objects.create(vote=vote, member=member, choice_key=key)
        rules.apply_fallback(vote.id)
        return Vote.objects.get(pk=vote.id)

    def current_shares(self):
        return {s.member.display_name: s for s in Share.objects.filter(purchase=self.purchase, superseded_by__isnull=True)
                .select_related("member")}

    def consent_for(self, share, cap):
        return Consent.objects.create(member=share.member, share=share, consent_text_version="v1", consent_text_sha256="0" * 64,
                                      amount_cents=share.amount_cents, cap_cents=cap, method="passkey", accepted_at=timezone.now())


class ProposeTests(FallbackBase):
    def test_fallback_math(self):
        """$1,050 between 5 people is $210; the cheaper Juniper Loft is $925 / 5 = $185; a replacement keeps $175."""
        self.hold_everyone_but_jordan()
        data = self.propose()
        by_kind = {o["key"]: o for o in data["options"]}
        self.assertEqual(list(by_kind), ["split_gap", "cheaper", "replacement"])
        self.assertEqual([(o["per_person_cents"], o["delta_cents"]) for o in by_kind.values()],
                         [(21000, 3500), (18500, 1000), (17500, 0)])
        self.assertEqual(by_kind["cheaper"]["title"], "Juniper Loft")
        self.assertEqual(data["staying"], 5)
        self.assertEqual(Vote.objects.get(pk=data["vote_id"]).purpose, "fallback")

    def test_cap_impact_says_how_many_never_who(self):
        self.hold_everyone_but_jordan()
        impact = {o["key"]: o["cap_impact"] for o in self.propose()["options"]}
        self.assertEqual(impact["cheaper"], "Within everyone's cap")                 # $185 is under the lowest cap, $190
        self.assertTrue(impact["split_gap"].startswith("Over "))                       # $210 is above some caps
        for text in impact.values():
            for name in self.members:
                self.assertNotIn(name, text)

    def test_choices_use_the_shape_the_chat_expects(self):
        self.hold_everyone_but_jordan()
        vote = Vote.objects.get(pk=self.propose()["vote_id"])
        self.assertEqual([(c["key"], c["option_id"]) for c in vote.choices],
                         [("1", "split_gap"), ("2", "cheaper"), ("3", "replacement")])
        self.assertEqual(vote.quorum, 3)                                              # majority of the 5 who are left
        self.assertTrue(all("label" in c and "per_person_cents" in c for c in vote.choices))

    def test_asking_twice_returns_the_same_vote(self):
        self.hold_everyone_but_jordan()
        self.assertEqual(self.propose()["vote_id"], self.propose()["vote_id"])
        self.assertEqual(Vote.objects.filter(purpose="fallback").count(), 1)

    def test_only_while_collecting(self):
        for name in self.parts:
            approve(name, self.parts)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "funded")
        result = self.engine.execute("propose_fallback", {"purchase_id": str(self.purchase.id), "reason": "price_change"},
                                     f"member:{self.members['Maya'].id}")
        self.assertEqual(result["error"]["code"], "illegal_state")

    def test_no_cheaper_option_when_nothing_fits(self):
        from merchant.models import Listing
        Listing.objects.filter(ref="juniper-loft").update(available=False)
        self.hold_everyone_but_jordan()
        self.assertEqual([o["key"] for o in self.propose()["options"]], ["split_gap", "replacement"])

    def test_dropping_out_asks_the_agent_for_fallbacks(self):
        with mock.patch("chat.services.on_share_declined") as notify, self.captureOnCommitCallbacks(execute=True):
            rules.member_withdraw(self.parts["Jordan"][0].id, "declined")
        notify.assert_called_once_with(self.group.id, self.purchase.id)


class VotingTests(FallbackBase):
    def test_only_people_still_in_can_vote(self):
        self.hold_everyone_but_jordan()
        vote = Vote.objects.get(pk=self.propose()["vote_id"])
        eligible = {m.display_name for m in consensus.eligible_voters(vote)}
        self.assertEqual(eligible, {"Maya", "Priya", "Sam", "Leo", "Alex"})

    def test_ballot_api_blocks_the_dropout_and_applies_the_winner(self):
        from rest_framework.test import APIClient
        self.hold_everyone_but_jordan()
        data = self.propose()
        client, vid = APIClient(), data["vote_id"]
        post = lambda name, key: client.post(f"/api/v1/votes/{vid}/ballots", {"choice_key": key}, format="json",
                                             HTTP_X_TANDEM_MEMBER=str(self.members[name].id))
        self.assertEqual(post("Jordan", "1").json()["error"]["code"], "not_a_voter")
        for name in ("Maya", "Priya"):
            post(name, "3")                                                           # replacement
        before = Purchase.objects.get(pk=self.purchase.pk).deadline_at
        self.assertEqual(Purchase.objects.get(pk=self.purchase.pk).deadline_at, before)       # 2 of 5: still open
        self.assertEqual(post("Sam", "3").status_code, 200)                           # 3 of 5 is a majority: closes and applies
        self.assertEqual(Purchase.objects.get(pk=self.purchase.pk).deadline_at, before + timedelta(hours=12))
        self.assertEqual(post("Leo", "1").json()["error"]["code"], "vote_closed")      # too late to change anything
        self.assertEqual(EventLog.objects.filter(type="fallback.applied").count(), 1)


class ApplyFallbackTests(FallbackBase):
    def test_replacement_adds_twelve_hours_and_changes_nothing_else(self):
        self.hold_everyone_but_jordan()
        before = self.purchase.deadline_at
        self.vote_all(self.propose()["vote_id"], "replacement")
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.deadline_at, before + timedelta(hours=12))
        self.assertEqual((self.purchase.version, self.purchase.total_cents), (1, 105000))
        self.assertEqual(sorted(s.status for s in self.current_shares().values()), ["declined"] + ["held"] * 5)

    def test_split_gap_replaces_shares_and_keeps_old_holds_until_confirmed(self):
        self.hold_everyone_but_jordan()
        vote = self.vote_all(self.propose()["vote_id"], "split_gap")
        self.assertEqual(vote.winner_key, "1")
        self.purchase.refresh_from_db()
        self.assertEqual((self.purchase.version, self.purchase.state, self.purchase.total_cents), (2, "collecting", 105000))
        shares = self.current_shares()
        self.assertEqual({n: (s.amount_cents, s.status) for n, s in shares.items() if n != "Jordan"},
                         {n: (21000, "viewing") for n in ("Maya", "Priya", "Sam", "Leo", "Alex")})
        self.assertEqual(sum(s.amount_cents for n, s in shares.items() if n != "Jordan"), 105000)
        # nobody's money moved yet: the old holds are untouched
        self.assertEqual(sorted(Hold.objects.filter(status="authorized").values_list("amount_cents", flat=True)), [17500] * 5)
        self.assertEqual(Share.objects.filter(status="held", superseded_by__isnull=False).count(), 5)
        self.assertEqual(EventLog.objects.filter(type="fallback.applied").count(), 1)

    def test_applying_twice_does_nothing_the_second_time(self):
        self.hold_everyone_but_jordan()
        vote = self.vote_all(self.propose()["vote_id"], "split_gap")
        self.assertEqual(rules.apply_fallback(vote.id)["status"], "already_applied")
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.version, 2)

    def test_cheaper_switches_the_listing(self):
        self.hold_everyone_but_jordan()
        self.vote_all(self.propose()["vote_id"], "cheaper")
        self.purchase.refresh_from_db()
        self.assertEqual((self.purchase.option.title, self.purchase.total_cents), ("Juniper Loft", 92500))
        self.assertEqual({s.amount_cents for n, s in self.current_shares().items() if n != "Jordan"}, {18500})

    def test_no_winner_means_no_change(self):
        self.hold_everyone_but_jordan()
        vote = Vote.objects.get(pk=self.propose()["vote_id"])
        Vote.objects.filter(pk=vote.pk).update(closes_at=timezone.now() - timedelta(minutes=1))     # time is up, nobody voted
        self.assertEqual(rules.apply_fallback(vote.id)["error"]["code"], "no_winner")
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.version, 1)

    def test_odd_cents_go_to_the_organizer(self):
        self.hold_everyone_but_jordan()
        Purchase.objects.filter(pk=self.purchase.pk).update(total_cents=105001)
        Option.objects.filter(pk=self.purchase.option_id).update(total_cents=105001)
        self.vote_all(self.propose()["vote_id"], "split_gap")
        amounts = {n: s.amount_cents for n, s in self.current_shares().items() if n != "Jordan"}
        self.assertEqual(sum(amounts.values()), 105001)
        self.assertEqual(amounts["Maya"], 21001)                                       # Maya organizes, so she carries the cent


class ShareChangeTests(FallbackBase):
    def test_share_increase_requires_reconfirm(self):
        """Nothing is adjusted until the member confirms. Then the SAME hold is adjusted (same listing, within cap)."""
        self.hold_everyone_but_jordan(cap=25000)                  # everyone approved up to $250, so $210 fits under it
        self.vote_all(self.propose()["vote_id"], "split_gap")
        maya = self.current_shares()["Maya"]
        self.assertEqual(maya.status, "viewing")
        self.assertEqual(Hold.objects.get(share__member=maya.member, status="authorized").amount_cents, 17500)   # untouched

        old_hold_id = Hold.objects.get(share__member=maya.member, status="authorized").gateway_hold_id
        result = rules.apply_share_change(maya.id, self.consent_for(maya, cap=25000).id)
        self.assertEqual((result["share_status"], result["amount_cents"]), ("held", 21000))
        hold = Hold.objects.get(share=maya, status="authorized")
        self.assertEqual((hold.gateway_hold_id, hold.amount_cents), (old_hold_id, 21000))      # same hold, new amount
        self.assertEqual(self.gateway.holds[old_hold_id]["amount"], 21000)
        self.assertEqual(Mandate.objects.filter(member=maya.member).count(), 1)                # no new mandate needed
        self.assertEqual(Share.objects.filter(member=maya.member, status="held", superseded_by__isnull=False).count(), 0)

    def test_everyone_confirming_funds_the_purchase(self):
        self.hold_everyone_but_jordan(cap=25000)
        self.vote_all(self.propose()["vote_id"], "split_gap")
        with self.captureOnCommitCallbacks(execute=False):
            for name, share in self.current_shares().items():
                if name != "Jordan":
                    result = rules.apply_share_change(share.id, self.consent_for(share, cap=25000).id)
                    self.assertEqual(result["share_status"], "held", result)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "funded")

    def test_over_cap_requires_new_mandate(self):
        """$210 is above the $190 limit these members approved. A tap is not enough; they need to approve a higher limit."""
        self.hold_everyone_but_jordan()                            # cap $190
        self.vote_all(self.propose()["vote_id"], "split_gap")
        maya = self.current_shares()["Maya"]
        refused = rules.apply_share_change(maya.id, self.consent_for(maya, cap=CAP_CENTS).id)
        self.assertEqual(refused["error"]["code"], "new_consent_required")
        self.assertEqual(Hold.objects.get(share__member=maya.member, status="authorized").amount_cents, 17500)

        old_mandate = Mandate.objects.get(member=maya.member, status="active")
        fresh = self.consent_for(maya, cap=22000)                  # the member approves a higher limit on the consent page
        result = rules.apply_share_change(maya.id, fresh.id)
        self.assertEqual((result["share_status"], result["amount_cents"]), ("held", 21000))
        mandates = Mandate.objects.filter(member=maya.member)
        self.assertEqual(mandates.count(), 2)                                              # a NEW mandate was created
        old_mandate.refresh_from_db()
        self.assertEqual(old_mandate.status, "revoked")                                    # and the old one revoked
        self.assertEqual(mandates.get(status="active").cap_cents, 22000)
        self.assertEqual(Hold.objects.filter(share__member=maya.member, status="authorized").count(), 1)   # old hold released
        self.assertEqual(Hold.objects.get(share__member=maya.member, status="authorized").amount_cents, 21000)

    def test_new_listing_needs_a_new_mandate_even_within_cap(self):
        """R6: Juniper Loft is a different listing, so the old mandate (locked to Pinecrest) cannot be reused."""
        self.hold_everyone_but_jordan(cap=25000)
        self.vote_all(self.propose()["vote_id"], "cheaper")
        sam = self.current_shares()["Sam"]
        old_mandate = Mandate.objects.get(member=sam.member, status="active")
        self.assertEqual(old_mandate.listing_ref, "pinecrest-a-frame")
        result = rules.apply_share_change(sam.id, self.consent_for(sam, cap=25000).id)
        self.assertEqual(result["share_status"], "held", result)
        new_mandate = Mandate.objects.get(member=sam.member, status="active")
        self.assertNotEqual(new_mandate.id, old_mandate.id)
        self.assertEqual(new_mandate.listing_ref, "juniper-loft")

    def test_confirming_twice_is_harmless(self):
        self.hold_everyone_but_jordan(cap=25000)
        self.vote_all(self.propose()["vote_id"], "split_gap")
        maya = self.current_shares()["Maya"]
        consent = self.consent_for(maya, cap=25000)
        rules.apply_share_change(maya.id, consent.id)
        again = rules.apply_share_change(maya.id, consent.id)
        self.assertEqual(again["share_status"], "held")
        self.assertEqual(Hold.objects.filter(share=maya, status="authorized").count(), 1)

    def test_consent_must_match_the_share(self):
        self.hold_everyone_but_jordan(cap=25000)
        self.vote_all(self.propose()["vote_id"], "split_gap")
        maya = self.current_shares()["Maya"]
        wrong = self.consent_for(maya, cap=25000)
        Consent.objects.filter(pk=wrong.pk).update(amount_cents=17500)
        self.assertEqual(rules.apply_share_change(maya.id, wrong.id)["error"]["code"], "consent_mismatch")
        self.assertEqual(rules.apply_share_change(maya.id, "00000000-0000-0000-0000-000000000000")["error"]["code"],
                         "consent_required")

    def test_after_the_deadline_nothing_changes(self):
        self.hold_everyone_but_jordan(cap=25000)
        self.vote_all(self.propose()["vote_id"], "split_gap")
        maya = self.current_shares()["Maya"]
        consent = self.consent_for(maya, cap=25000)
        Purchase.objects.filter(pk=self.purchase.pk).update(deadline_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(rules.apply_share_change(maya.id, consent.id)["error"]["code"], "deadline_passed")


class MoneyCleanupTests(FallbackBase):
    def test_deadline_release_frees_old_holds_of_replaced_shares(self):
        self.hold_everyone_but_jordan()
        self.vote_all(self.propose()["vote_id"], "split_gap")          # shares replaced, old holds still alive
        result = rules.release_all(self.purchase.id, "deadline")
        self.assertEqual(result["holds_released"], 5)
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 0)
        self.assertEqual({h["status"] for h in self.gateway.holds.values()}, {"RELEASED"})
        self.assertEqual(Share.objects.filter(purchase=self.purchase, status="held").count(), 0)

    def test_backing_out_after_a_fallback_frees_the_old_hold(self):
        self.hold_everyone_but_jordan()
        self.vote_all(self.propose()["vote_id"], "split_gap")
        sam = self.current_shares()["Sam"]
        result = rules.member_withdraw(sam.id, "member_left")
        self.assertEqual(result["share_status"], "declined")
        self.assertEqual(Hold.objects.filter(share__member=sam.member, status="authorized").count(), 0)
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 4)

    def test_approving_on_the_consent_page_replaces_the_old_hold(self):
        """The consent link calls place_hold on the new share. The old hold must not be left behind."""
        self.hold_everyone_but_jordan()
        self.vote_all(self.propose()["vote_id"], "cheaper")
        leo = self.current_shares()["Leo"]
        consent = self.consent_for(leo, cap=22000)
        token = leo.member.agent_tokens.first()
        result = rules.place_hold(leo.id, consent.id, token.id, 22000)
        self.assertEqual(result["share_status"], "held", result)
        holds = Hold.objects.filter(share__member=leo.member, status="authorized")
        self.assertEqual([h.amount_cents for h in holds], [18500])
