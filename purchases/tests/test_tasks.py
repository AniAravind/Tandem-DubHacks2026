"""
Tests for the background jobs (milestone M7).

The jobs are plain functions, so we call them directly. To control time we replace `tasks._now` with a fixed clock.
Same world as the other money tests: six members, a $1,050.00 purchase, six $175.00 shares, the deadline in 3 days.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from chat.models import ChatMessage
from core.models import EventLog, Group
from payments.models import Hold
from payments.simulated import SimulatedVisaAgentGateway
from purchases import rules, tasks
from purchases.models import Purchase, Share, Vote
from purchases.tests.test_checkout import TEST_SIGNING_KEY_B64, CountingMerchant
from purchases.tests.test_rules_holds import approve, build_world

import os


class TaskBase(TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"AGENT_SIGNING_KEY_B64": TEST_SIGNING_KEY_B64})
        env.start()
        self.addCleanup(env.stop)
        cache.clear()
        self.gateway = SimulatedVisaAgentGateway(latency_ms=0)
        self.merchant = CountingMerchant()
        for target, value in (("purchases.rules.get_gateway", self.gateway), ("purchases.tasks.get_gateway", self.gateway),
                              ("purchases.rules.get_merchant_client", self.merchant)):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.purchase, self.parts = build_world(self.gateway, all_cards_ok=True)
        Group.objects.update(timezone="America/Los_Angeles")

    def set_clock(self, when):
        patcher = mock.patch("purchases.tasks._now", return_value=when)
        patcher.start()
        self.addCleanup(patcher.stop)

    def clock_before_deadline(self, delta: timedelta, local_hour=None):
        """Pretend it is `delta` before the deadline. If local_hour is given, move the DEADLINE so the clock reads that hour."""
        if local_hour is not None:
            from zoneinfo import ZoneInfo
            now = datetime(2026, 7, 14, local_hour, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
            self.purchase.deadline_at = now + delta
            self.purchase.save(update_fields=["deadline_at"])
        else:
            now = self.purchase.deadline_at - delta
        self.set_clock(now.astimezone(dt_timezone.utc))

    def share(self, name):
        return self.parts[name][0]

    def reminders(self, name=None):
        events = EventLog.objects.filter(type="reminder.sent")
        return [e for e in events if name is None or e.payload["share_id"] == str(self.share(name).id)]

    def private_messages(self, name):
        return ChatMessage.objects.filter(member=self.share(name).member, direction="out")


# ---------------------------------------------------------------------------------------- deadline
class ExpirePurchaseTests(TaskBase):
    def hold_three(self):
        for name in list(self.parts)[:3]:
            approve(name, self.parts)

    def test_deadline_releases_all_holds(self):
        self.hold_three()
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 3)
        self.purchase.deadline_at = timezone.now() - timedelta(minutes=1)
        self.purchase.save(update_fields=["deadline_at"])
        self.assertEqual(tasks.expire_purchase(str(self.purchase.id)), "expired")
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "released")
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 0)
        self.assertEqual(Hold.objects.filter(status="released").count(), 3)
        self.assertEqual(self.gateway.captures, {})                              # nobody was charged
        self.assertTrue(ChatMessage.objects.filter(text_redacted__contains="Nobody was charged").exists())

    def test_running_it_twice_changes_nothing(self):
        self.hold_three()
        self.purchase.deadline_at = timezone.now() - timedelta(minutes=1)
        self.purchase.save(update_fields=["deadline_at"])
        tasks.expire_purchase(str(self.purchase.id))
        events_before = EventLog.objects.count()
        messages_before = ChatMessage.objects.count()
        self.assertEqual(tasks.expire_purchase(str(self.purchase.id)), "skipped")
        self.assertEqual((EventLog.objects.count(), ChatMessage.objects.count()), (events_before, messages_before))

    def test_before_the_deadline_it_does_nothing(self):
        self.hold_three()
        self.assertEqual(tasks.expire_purchase(str(self.purchase.id)), "not_yet")
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "collecting")
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 3)

    def test_a_funded_purchase_is_never_expired(self):
        for name in self.parts:
            approve(name, self.parts)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "funded")
        self.purchase.deadline_at = timezone.now() - timedelta(minutes=1)
        self.purchase.save(update_fields=["deadline_at"])
        self.assertEqual(tasks.expire_purchase(str(self.purchase.id)), "skipped")
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 6)

    def test_unknown_purchase_is_skipped(self):
        self.assertEqual(tasks.expire_purchase("00000000-0000-0000-0000-000000000000"), "skipped")


# ---------------------------------------------------------------------------------------- reminders
class ReminderTests(TaskBase):
    def test_reminder_goes_privately_to_one_person(self):
        self.clock_before_deadline(timedelta(hours=2), local_hour=12)
        self.assertEqual(tasks.send_reminder(str(self.share("Maya").id), "2h"), "sent")
        self.assertEqual(len(self.reminders("Maya")), 1)
        mine = self.private_messages("Maya")
        self.assertEqual(mine.count(), 1)
        self.assertIn("About 2 hours", mine.get().text_redacted)
        self.assertIn("/c/", mine.get().text_redacted)                           # their own link
        self.assertEqual(ChatMessage.objects.filter(member__isnull=True).count(), 0)   # nothing went to the whole group

    def test_reminders_skip_members_who_already_held(self):
        approve("Maya", self.parts)
        self.clock_before_deadline(timedelta(hours=2), local_hour=12)
        self.assertEqual(tasks.send_reminder(str(self.share("Maya").id), "2h"), "skipped")
        self.assertEqual(self.reminders("Maya"), [])
        self.assertEqual(tasks.send_reminder(str(self.share("Priya").id), "2h"), "sent")      # but Priya still gets one

    def test_same_tier_is_never_sent_twice(self):
        self.clock_before_deadline(timedelta(hours=2), local_hour=12)
        share_id = str(self.share("Sam").id)
        self.assertEqual(tasks.send_reminder(share_id, "2h"), "sent")
        self.assertEqual(tasks.send_reminder(share_id, "2h"), "already_sent")
        self.assertEqual(len(self.reminders("Sam")), 1)

    def test_at_most_three_reminders(self):
        share_id = str(self.share("Sam").id)
        for tier in ("12h", "2h", "30m"):
            self.clock_before_deadline(tasks.TIER_WINDOW[tier], local_hour=12)
            self.assertEqual(tasks.send_reminder(share_id, tier), "sent")
        self.assertEqual(len(self.reminders("Sam")), 3)
        # even a made-up 4th reminder is refused
        with mock.patch.dict(tasks.TIER_WINDOW, {"5m": timedelta(minutes=5)}):
            self.clock_before_deadline(timedelta(minutes=5), local_hour=12)
            self.assertEqual(tasks.send_reminder(share_id, "5m"), "max_reached")

    def test_too_early_and_too_late(self):
        share_id = str(self.share("Sam").id)
        self.clock_before_deadline(timedelta(hours=5), local_hour=12)             # 5h left: the 2h reminder is not due
        self.assertEqual(tasks.send_reminder(share_id, "2h"), "too_early")
        self.clock_before_deadline(timedelta(minutes=-1), local_hour=12)          # deadline already passed
        self.assertEqual(tasks.send_reminder(share_id, "30m"), "too_late")
        self.assertEqual(self.reminders(), [])

    def test_unknown_tier_and_missing_share(self):
        self.assertEqual(tasks.send_reminder(str(self.share("Sam").id), "99h"), "unknown_tier")
        self.assertEqual(tasks.send_reminder("00000000-0000-0000-0000-000000000000", "2h"), "gone")

    def test_quiet_hours_wait_until_morning(self):
        self.clock_before_deadline(timedelta(hours=12), local_hour=2)             # 2:30 AM, deadline is mid-afternoon
        with mock.patch("purchases.tasks.schedule") as queued:
            self.assertEqual(tasks.send_reminder(str(self.share("Sam").id), "12h"), "delayed_quiet_hours")
        self.assertEqual(self.reminders(), [])
        task, share_id, tier = queued.call_args.args[:3]
        wake = queued.call_args.kwargs["eta"]
        self.assertEqual((task, share_id, tier), (tasks.send_reminder, str(self.share("Sam").id), "12h"))
        from zoneinfo import ZoneInfo
        self.assertEqual(wake.astimezone(ZoneInfo("America/Los_Angeles")).hour, 8)

    def test_quiet_hours_with_an_earlier_deadline_stay_quiet(self):
        self.clock_before_deadline(timedelta(minutes=30), local_hour=2)           # deadline 3:00 AM, before 8 AM
        with mock.patch("purchases.tasks.schedule") as queued:
            self.assertEqual(tasks.send_reminder(str(self.share("Sam").id), "30m"), "quiet_hours")
        queued.assert_not_called()
        self.assertEqual(self.reminders(), [])

    def test_quiet_hours_boundaries(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
        self.assertTrue(tasks.in_quiet_hours(datetime(2026, 7, 14, 23, 0, tzinfo=tz)))
        self.assertTrue(tasks.in_quiet_hours(datetime(2026, 7, 14, 7, 59, tzinfo=tz)))
        self.assertFalse(tasks.in_quiet_hours(datetime(2026, 7, 14, 8, 0, tzinfo=tz)))
        self.assertFalse(tasks.in_quiet_hours(datetime(2026, 7, 14, 22, 59, tzinfo=tz)))


# ---------------------------------------------------------------------------------------- votes
class CloseVoteTests(TaskBase):
    def make_vote(self, purpose="pick_option", minutes=-1, **extra):
        return Vote.objects.create(
            group=self.purchase.group, purpose=purpose, quorum=4, closes_at=timezone.now() + timedelta(minutes=minutes),
            choices=[{"key": "1", "option_id": "a", "label": "Cabin A"}, {"key": "2", "option_id": "b", "label": "Cabin B"}],
            **extra)

    def test_a_finished_vote_is_closed_and_announced(self):
        from purchases.models import Ballot
        vote = self.make_vote()
        for name in ("Maya", "Priya", "Sam", "Leo"):                             # 4 = the quorum
            Ballot.objects.create(vote=vote, member=self.share(name).member, choice_key="1")
        with mock.patch("chat.services.schedule_agent") as agent:
            self.assertEqual(tasks.close_vote(str(vote.id)), "closed")
        vote.refresh_from_db()
        self.assertEqual((vote.status, vote.winner_key), ("closed", "1"))
        self.assertTrue(ChatMessage.objects.filter(text_redacted__contains="Cabin A wins, 4 votes to 0").exists())
        self.assertEqual(agent.call_args.args[2]["type"], "vote_closed")

    def test_running_it_twice_announces_once(self):
        vote = self.make_vote()
        with mock.patch("chat.services.schedule_agent"):
            tasks.close_vote(str(vote.id))
            count = ChatMessage.objects.count()
            self.assertEqual(tasks.close_vote(str(vote.id)), "already_closed")
        self.assertEqual(ChatMessage.objects.count(), count)

    def test_too_early_leaves_the_vote_open(self):
        vote = self.make_vote(minutes=30)
        self.assertEqual(tasks.close_vote(str(vote.id)), "still_open")
        vote.refresh_from_db()
        self.assertEqual(vote.status, "open")

    def test_unknown_vote(self):
        self.assertEqual(tasks.close_vote("00000000-0000-0000-0000-000000000000"), "gone")

    def test_a_vote_nobody_answered_ends_with_no_winner(self):
        vote = self.make_vote()
        tasks.close_vote(str(vote.id))
        self.assertTrue(ChatMessage.objects.filter(text_redacted__contains="without a winner").exists())

    def test_a_winning_fallback_is_applied(self):
        from purchases.models import Ballot
        before = self.purchase.deadline_at
        approve("Maya", self.parts)
        vote = Vote.objects.create(
            group=self.purchase.group, purchase=self.purchase, purpose="fallback", quorum=4,
            closes_at=timezone.now() - timedelta(minutes=1),
            choices=[{"key": "1", "option_id": "replacement", "label": "Invite someone", "per_person_cents": 17500,
                      "delta_cents": 0, "cap_impact": "x"}])
        for name in ("Maya", "Priya", "Sam", "Leo"):
            Ballot.objects.create(vote=vote, member=self.share(name).member, choice_key="1")
        tasks.close_vote(str(vote.id))
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.deadline_at, before + timedelta(hours=12))
        tasks.close_vote(str(vote.id))                                           # a repeat must not extend it again
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.deadline_at, before + timedelta(hours=12))

    def test_cancelling_a_price_change_releases_the_holds(self):
        from purchases.models import Ballot
        approve("Maya", self.parts)
        vote = self.make_vote(purpose="price_change", purchase=self.purchase)
        vote.choices = [{"key": "accept", "label": "Pay the new price"}, {"key": "cancel", "label": "Cancel the purchase"}]
        vote.save()
        for name in ("Maya", "Priya", "Sam", "Leo"):
            Ballot.objects.create(vote=vote, member=self.share(name).member, choice_key="cancel")
        tasks.close_vote(str(vote.id))
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "released")
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 0)


# ---------------------------------------------------------------------------------------- checkout retries
class RetryCheckoutTests(TaskBase):
    def fund(self):
        for name in self.parts:
            approve(name, self.parts)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "funded")

    def merchant_down(self):
        self.merchant.fail = {"agent/checkout": (503, {"error": {"code": "merchant_down", "message": "try later"}})}

    def test_a_temporary_failure_is_retried_with_longer_waits(self):
        self.fund()
        self.merchant_down()
        with mock.patch("purchases.tasks.schedule") as queued:
            self.assertEqual(tasks.retry_checkout(str(self.purchase.id), 1), "retry_scheduled")
            self.assertEqual(queued.call_args.args[:3], (tasks.retry_checkout, str(self.purchase.id), 2))
            self.assertEqual(queued.call_args.kwargs["countdown"], 30)
            tasks.retry_checkout(str(self.purchase.id), 2)
            self.assertEqual(queued.call_args.kwargs["countdown"], 60)
        self.assertEqual(self.gateway.captures, {})

    def test_it_gives_up_after_three_tries_and_keeps_the_holds(self):
        self.fund()
        self.merchant_down()
        with mock.patch("purchases.tasks.schedule") as queued:
            self.assertEqual(tasks.retry_checkout(str(self.purchase.id), tasks.MAX_CHECKOUT_RETRIES), "gave_up")
        queued.assert_not_called()
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 6)
        self.assertEqual(self.gateway.captures, {})
        self.assertTrue(ChatMessage.objects.filter(text_redacted__contains="not answering").exists())

    def test_a_retry_that_works_books_once(self):
        self.fund()
        self.merchant_down()
        rules.checkout(self.purchase.id)                                         # first try fails
        self.merchant.fail = {}                                                  # the merchant recovers
        self.assertEqual(tasks.retry_checkout(str(self.purchase.id), 2), "booked")
        self.assertEqual(tasks.retry_checkout(str(self.purchase.id), 3), "skipped")   # already booked: nothing more happens
        self.assertEqual(len(self.gateway.captures), 6)

    def test_the_booking_key_is_the_same_on_every_try(self):
        self.fund()
        self.merchant_down()
        keys = []
        real_send = self.merchant.send

        def spy(slug, action, headers, body):
            if action == "agent/checkout":
                keys.append(headers["Idempotency-Key"])
            return real_send(slug, action, headers, body)

        self.merchant.send = spy
        rules.checkout(self.purchase.id)
        rules.checkout(self.purchase.id)
        self.assertEqual(len(keys), 4)                                           # 2 tries each, 2 checkouts
        self.assertEqual(len(set(keys)), 1)                                      # always the same key

    def test_real_rejections_are_not_retried(self):
        self.fund()
        self.merchant.fail = {"agent/checkout": (409, {"error": {"code": "sold_out", "message": "gone"}})}
        with mock.patch("purchases.tasks.schedule") as queued:
            self.assertEqual(tasks.retry_checkout(str(self.purchase.id), 1), "rejected")
        queued.assert_not_called()

    def test_funding_the_purchase_queues_a_retry_when_booking_hits_trouble(self):
        for name in list(self.parts)[:5]:
            approve(name, self.parts)
        self.merchant_down()
        with mock.patch("purchases.tasks.schedule") as queued:
            with self.captureOnCommitCallbacks(execute=True):
                approve(list(self.parts)[5], self.parts)
        self.assertEqual(queued.call_args.args[:3], (tasks.retry_checkout, str(self.purchase.id), 2))


# ---------------------------------------------------------------------------------------- hold refresh
class RefreshHoldTests(TaskBase):
    def near_expiry_hold(self):
        approve("Maya", self.parts)
        hold = Hold.objects.get(share=self.share("Maya"))
        hold.expires_at = timezone.now() + timedelta(hours=6)
        hold.save(update_fields=["expires_at"])
        return hold

    def test_an_expiring_hold_is_replaced(self):
        hold = self.near_expiry_hold()
        self.assertEqual(tasks.refresh_hold(str(hold.id)), "refreshed")
        hold.refresh_from_db()
        self.assertEqual(hold.status, "released")
        fresh = Hold.objects.get(share=self.share("Maya"), status="authorized")
        self.assertNotEqual(fresh.id, hold.id)
        self.assertEqual(fresh.amount_cents, hold.amount_cents)
        self.assertEqual(EventLog.objects.filter(type="hold.refreshed").count(), 1)
        self.assertEqual(self.gateway.captures, {})

    def test_a_fresh_hold_is_left_alone(self):
        approve("Maya", self.parts)
        hold = Hold.objects.get(share=self.share("Maya"))
        hold.expires_at = timezone.now() + timedelta(days=7)
        hold.save(update_fields=["expires_at"])
        self.assertEqual(tasks.refresh_hold(str(hold.id)), "not_yet")
        hold.refresh_from_db()
        self.assertEqual(hold.status, "authorized")

    def test_running_it_twice_replaces_the_hold_once(self):
        hold = self.near_expiry_hold()
        tasks.refresh_hold(str(hold.id))
        self.assertEqual(tasks.refresh_hold(str(hold.id)), "skipped")            # the old hold is already released
        self.assertEqual(Hold.objects.filter(share=self.share("Maya"), status="authorized").count(), 1)

    def test_a_failed_refresh_keeps_the_old_hold(self):
        hold = self.near_expiry_hold()
        from purchases.models import Purchase as P_
        mandate = hold.mandate
        mandate.expires_at = timezone.now() - timedelta(minutes=1)
        mandate.save(update_fields=["expires_at"])
        self.assertEqual(tasks.refresh_hold(str(hold.id)), "failed")
        hold.refresh_from_db()
        self.assertEqual(hold.status, "authorized")
        self.assertEqual(EventLog.objects.filter(type="hold.refresh_failed").count(), 1)


# ---------------------------------------------------------------------------------------- sweep and scheduling
class SweepTests(TaskBase):
    def test_sweep_expires_and_closes_what_is_due(self):
        approve("Maya", self.parts)
        self.purchase.deadline_at = timezone.now() - timedelta(minutes=1)
        self.purchase.save(update_fields=["deadline_at"])
        vote = Vote.objects.create(group=self.purchase.group, purpose="pick_option", quorum=4,
                                   closes_at=timezone.now() - timedelta(minutes=1),
                                   choices=[{"key": "1", "option_id": "a", "label": "A"}])
        with mock.patch("chat.services.schedule_agent"):
            done = tasks.sweep()
        self.assertEqual((done["votes"], done["purchases"]), (1, 1))
        vote.refresh_from_db()
        self.assertEqual(vote.status, "closed")
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "released")

    def test_sweep_does_nothing_when_nothing_is_due(self):
        self.assertEqual(tasks.sweep(), {"votes": 0, "purchases": 0, "reminders": 0, "holds": 0})

    def test_sweep_sends_the_most_urgent_reminder_once(self):
        self.clock_before_deadline(timedelta(minutes=20), local_hour=12)          # the 12h, 2h and 30m marks have all passed
        first = tasks.sweep()
        self.assertEqual(first["reminders"], 6)
        for name in self.parts:
            self.assertEqual(len(self.reminders(name)), 1)                        # one nudge each, not three at once
            self.assertEqual(self.reminders(name)[0].payload["tier"], "30m")
        self.assertEqual(tasks.sweep()["reminders"], 0)                           # a second sweep sends nothing new

    def test_creating_a_purchase_queues_its_jobs(self):
        with mock.patch("purchases.tasks.schedule") as queued:
            tasks.schedule_purchase_jobs(self.purchase, list(self.purchase.shares.all()))
        names = [c.args[0].name for c in queued.call_args_list]
        self.assertEqual(names.count("purchases.tasks.expire_purchase"), 1)
        self.assertEqual(names.count("purchases.tasks.send_reminder"), 18)       # 6 people x 3 reminders
        etas = [c.kwargs["eta"] for c in queued.call_args_list if c.args[0] is tasks.expire_purchase]
        self.assertEqual(etas, [self.purchase.deadline_at])

    def test_reminders_that_would_already_be_in_the_past_are_not_queued(self):
        self.purchase.deadline_at = timezone.now() + timedelta(hours=1)
        self.purchase.save(update_fields=["deadline_at"])
        with mock.patch("purchases.tasks.schedule") as queued:
            tasks.schedule_purchase_jobs(self.purchase, [self.share("Sam")])
        tiers = [c.args[2] for c in queued.call_args_list if c.args[0] is tasks.send_reminder]
        self.assertEqual(tiers, ["30m"])                                          # only the 30-minute mark is still ahead

    def test_beat_runs_the_sweep_every_minute(self):
        from django.conf import settings
        entry = settings.CELERY_BEAT_SCHEDULE["tandem-sweep"]
        self.assertEqual((entry["task"], entry["schedule"]), ("purchases.tasks.sweep", 60.0))


# ---------------------------------------------------------------------------------------- other M7 fixes
class DeliveryAndGatewayTests(TaskBase):
    def test_retry_delay_doubles(self):
        self.assertEqual([tasks.retry_delay(n) for n in (1, 2, 3)], [30, 60, 120])

    def test_each_consent_link_goes_only_to_its_owner(self):
        shares = list(self.purchase.shares.select_related("member", "purchase__option", "purchase__group"))
        self.assertEqual(rules._send_consent_links(shares), 6)
        for share in shares:
            mine = ChatMessage.objects.filter(member=share.member, direction="out", text_redacted__contains="/c/")
            self.assertEqual(mine.count(), 1)
            self.assertIn(rules.consent_link(share).split("/c/")[1][:20], mine.get().text_redacted)

    def test_a_chat_failure_never_breaks_money_logic(self):
        shares = list(self.purchase.shares.select_related("member", "purchase__option", "purchase__group"))
        with mock.patch("chat.services.post_card", side_effect=RuntimeError("chat is down")), \
                self.assertLogs("purchases.rules", level="ERROR"):                # the failure is logged, not raised
            self.assertEqual(rules._send_consent_links(shares), 0)

    def test_get_gateway_rebuilds_state_from_the_database(self):
        """The web process and the worker have separate memory, so the worker must re-learn holds from the database."""
        from payments import gateway as gw
        approve("Maya", self.parts)
        hold = Hold.objects.get(share=self.share("Maya"))
        fresh = SimulatedVisaAgentGateway(latency_ms=0)
        self.assertNotIn(hold.gateway_hold_id, fresh.holds)
        with mock.patch.object(SimulatedVisaAgentGateway, "default", return_value=fresh):
            self.assertIs(gw.get_gateway.__wrapped__() if hasattr(gw.get_gateway, "__wrapped__") else gw.get_gateway(), fresh)
        self.assertIn(hold.gateway_hold_id, fresh.holds)
