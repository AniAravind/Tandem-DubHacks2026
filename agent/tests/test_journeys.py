"""Multi-turn private buying journeys through the real chat entry point, rules engine and database.

The model is scripted (no network), so these pin down OUR behavior at each conversation transition: topic changes,
stale choices, provider outages, repeated tool calls, and the server-written replies that save quota.
"""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from openai import RateLimitError, BadRequestError
import httpx

from agent import llm
from agent.llm import ProviderUnavailable
from agent.tests.helpers import reply, seed, tool_call
from chat import services
from chat.adapters.base import InboundMessage
from chat.models import ChatMessage, PhotonSession
from core.models import Group, Member
from purchases.models import Option, Purchase, TripPlan
from purchases.testing import assert_no_money_state_change
from core.models import EventLog


class Model:
    """Scripted provider: each user turn pops one list of responses (tool calls / text / exceptions)."""

    def __init__(self):
        self.turns: list[list] = []
        self.calls = 0

    def __call__(self, messages, tools, **kw):
        self.calls += 1
        if not self.turns or not self.turns[0]:
            return reply("Anything else?")
        r = self.turns[0].pop(0)
        if not self.turns[0]:
            self.turns.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "journeys"}})
@override_settings(PRIVATE_LIVE_ONLY=False)
class PrivateJourneyTests(TestCase):
    def setUp(self):
        cache.clear()
        seed()
        call_command("seed_buying_catalog", verbosity=0)
        self.group = Group.objects.create(name="Tandem · personal agent", channel="imessage")
        self.member = Member.objects.create(group=self.group, display_name="Planner", role="organizer",
                                            phone_e164_hash=hashlib.sha256(b"j").hexdigest())
        self.group.organizer = self.member
        self.group.save()
        PhotonSession.objects.create(key="journey", member=self.member)
        self.model = Model()
        patcher = patch("agent.llm.chat", self.model)
        patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, text, *responses):
        """One user message through the same path the Photon bridge uses. Returns the new outbound texts."""
        self.model.turns = [list(responses)] if responses else []
        before = ChatMessage.objects.filter(group=self.group, direction="out").order_by("-seq").values_list("seq", flat=True).first() or 0
        msg = InboundMessage(str(self.group.id), str(self.member.id), text, "imessage")
        stored = services.store_inbound(msg)
        vote = services.VOTE_REPLY.match(text)
        if not (vote and services.handle_vote_reply(msg.group_id, msg.member_id, vote.group(1))):
            services.run_agent(msg.group_id, msg.member_id, None, str(stored.id))
        return list(ChatMessage.objects.filter(group=self.group, direction="out", seq__gt=before).order_by("seq"))

    def brief(self, **args):
        return reply(calls=[tool_call("save_buying_brief", args)])

    def search(self, **args):
        return reply(calls=[tool_call("search_options", args)])

    def test_trip_then_topic_change_to_gift_never_reuses_stale_choices(self):
        out = self.send("Tahoe cabin Nov 14-16 for me and 5 friends, $300 each",
                        self.brief(goal="Tahoe cabin weekend", category="rental", group_size=6, budget_per_person=300),
                        self.search(category="rental", group_size=6, budget_per_person_max=300,
                                    dates={"start": "2026-11-14", "end": "2026-11-16"}))
        self.assertIn("Your shortlist", out[0].text_redacted)
        self.assertEqual([m.card["type"] for m in out if m.card], ["OptionCard"] * 3)
        self.assertEqual(self.model.calls, 3)                          # model inspects results before deciding to finish
        cabin_ids = set(Option.objects.filter(group=self.group).values_list("id", flat=True))

        out = self.send("actually forget the trip. birthday gift for my sister, just me, $40",
                        self.brief(goal="Birthday gift for sister", category="gift_bundle", group_size=1, budget_per_person=40),
                        reply("What does she enjoy? A hobby or two helps me pick."))
        self.assertIn("What does she enjoy", out[-1].text_redacted)
        state=PhotonSession.objects.get(member=self.member).planning_state
        self.assertIn("What does she enjoy", state["_dialogue"]["question"])
        calls=self.model.calls
        out = self.send("1")  # A detail answer goes to the model, never to the obsolete cabin shortlist.
        self.assertEqual(self.model.calls,calls+1)
        self.assertFalse(TripPlan.objects.exists())

        out = self.send("she loves baking", self.search(category="gift_bundle", group_size=1, budget_per_person_max=40))
        self.assertIn("Your shortlist", out[0].text_redacted)
        out = self.send("1")
        plan = TripPlan.objects.get()
        self.assertNotIn(plan.selected_option.meta.get("source_option_id"), {str(i) for i in cabin_ids})
        self.assertEqual(plan.expected_members, 1)
        self.assertTrue(any("?owner=" in (m.text_redacted + str(m.card)) or m.member_id for m in out))

    def test_provider_outage_keeps_shortlist_usable_and_honest(self):
        self.send("cabin for 6 in tahoe, $300 each",
                  self.brief(goal="Tahoe cabin", category="rental", group_size=6, budget_per_person=300),
                  self.search(category="rental", group_size=6, budget_per_person_max=300))
        since = EventLog.objects.order_by("-seq").values_list("seq", flat=True).first()
        out = self.send("which one is closest to the slopes?", ProviderUnavailable(480), ProviderUnavailable(480))
        text = out[0].text_redacted
        self.assertIn("busy for about 8 min", text)
        self.assertIn("reply 1, 2, 3", text)
        self.assertNotIn("$", text)
        assert_no_money_state_change(self.group.id, since)
        out = self.send("2")                                           # fast path needs no model
        self.assertTrue(TripPlan.objects.filter(source_group=self.group).exists())

    def test_repeated_identical_tool_call_is_not_rerun(self):
        args = dict(goal="Couch", category="furniture", group_size=4, budget_per_person=300)
        out = self.send("couch for 4 roommates, $300 each", self.brief(**args), self.brief(**args), self.brief(**args),
                        reply("What size couch, and do you need a sleeper?"))
        self.assertIn("What size couch", out[-1].text_redacted)
        saves = EventLog.objects.filter(group_id=self.group.id, type="agent.tool_called", payload__tool="save_buying_brief").count()
        self.assertEqual(saves, 1)

    def test_invented_price_reply_is_replaced_by_a_concrete_question(self):
        out = self.send("help me buy a telescope",
                        self.brief(goal="Telescope", category="telescope", group_size=1),
                        reply("Great! The Celestron NexStar is $499 and in stock."),
                        reply("Great! The Celestron NexStar is $499 and in stock."),
                        reply("Great! The Celestron NexStar is $499 and in stock."))
        self.assertNotIn("$499", out[-1].text_redacted)
        self.assertIn("what budget should I use", out[-1].text_redacted)

    def test_older_queued_turn_is_dropped_when_user_already_moved_on(self):
        first = services.store_inbound(InboundMessage(str(self.group.id), str(self.member.id), "cabin in tahoe", "imessage"))
        services.store_inbound(InboundMessage(str(self.group.id), str(self.member.id), "for 6 people", "imessage"))
        services.run_agent(str(self.group.id), str(self.member.id), None, str(first.id))
        self.assertEqual(self.model.calls, 0)
        self.assertFalse(ChatMessage.objects.filter(group=self.group, direction="out").exists())
        self.assertFalse(Purchase.objects.exists())


def _rate_limit(seconds):
    response = SimpleNamespace(headers={"retry-after": str(seconds)}, status_code=429, request=None)
    return RateLimitError("rate limited", response=response, body=None)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "llm"}})
class ModelFallbackTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_rejected_tool_generation_uses_fallback(self):
        rejected = BadRequestError("Invalid tool call", response=httpx.Response(400,
            request=httpx.Request("POST", "https://provider.invalid")),
            body={"code": "tool_use_failed", "failed_generation": "untrusted"})
        seen, client = self.fake({"a": [rejected]})
        with patch.object(llm, "client", client), patch.object(llm, "MODEL", "a"), \
                patch.object(llm, "FALLBACK_MODELS", ["b"]):
            self.assertEqual(llm.chat([], []).model, "b")
        self.assertEqual([m for m, *_ in seen], ["a", "b"])

    def test_native_rejection_regenerates_structured_action_without_failed_payload(self):
        rejected = BadRequestError("Invalid tool call", response=httpx.Response(400,
            request=httpx.Request("POST", "https://provider.invalid")),
            body={"code":"tool_use_failed","failed_generation":"DO NOT EXECUTE THIS"})
        response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(
            content=json.dumps({'content':'','tool_calls':[{'name':'save_buying_brief','arguments':'{"group_size":5}'}]})))])
        requests=[]
        def create(**kwargs):
            requests.append(kwargs)
            if len(requests)==1: raise rejected
            return response
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        tools=[{'type':'function','function':{'name':'save_buying_brief','parameters':{'type':'object'}}}]
        with patch.object(llm,'client',client),patch.object(llm,'MODEL','openai/gpt-oss-120b'), \
             patch.object(llm,'FALLBACK_MODELS',[]),patch.object(llm,'_GROQ',True):
            result=llm.chat([{'role':'user','content':'A trip for 5'}],tools)
        self.assertEqual(result.choices[0].message.tool_calls[0].function.name,'save_buying_brief')
        self.assertEqual(result.choices[0].message.tool_calls[0].function.arguments,'{"group_size":5}')
        self.assertTrue(requests[1]['response_format']['json_schema']['strict'])
        self.assertNotIn('tools',requests[1])
        self.assertNotIn('DO NOT EXECUTE THIS',json.dumps(requests[1]))

    def test_structured_recovery_cannot_call_unknown_or_forbidden_tools(self):
        response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(
            content=json.dumps({'content':'','tool_calls':[{'name':'charge_card','arguments':'{}'}]})))])
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs:response)))
        with patch.object(llm,'client',client):
            with self.assertRaises(ValueError): llm._structured_recovery('a',[],[],'auto')
            with self.assertRaises(ValueError): llm._structured_recovery('a',[],[{'function':{'name':'charge_card'}}],'none')

    def test_final_reply_uses_content_only_schema_without_native_tools(self):
        requests=[]
        response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='{"content":"Your dates are saved. What is your budget?"}'))])
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:requests.append(kw) or response)))
        with patch.object(llm,'client',client),patch.object(llm,'MODEL','openai/gpt-oss-120b'),patch.object(llm,'_GROQ',True):
            result=llm.chat([{'role':'user','content':'Continue'}],[{'function':{'name':'save_buying_brief'}}],tool_choice='none')
        self.assertIsNone(result.choices[0].message.tool_calls)
        self.assertIn('dates are saved',result.choices[0].message.content)
        self.assertNotIn('tools',requests[0])
        self.assertEqual(requests[0]['response_format']['json_schema']['schema']['required'],['content'])

    def test_key_rotation_does_not_inherit_old_cooldown(self):
        with patch.dict("os.environ", {"XAI_API_KEY": "old"}):
            llm.cool_down("a", 600)
            self.assertGreater(llm.cooling_until("a"), 0)
        with patch.dict("os.environ", {"XAI_API_KEY": "new"}):
            self.assertEqual(llm.cooling_until("a"), 0)

    def fake(self, outcomes):
        seen = []
        def create(model, **kw):
            seen.append((model, kw.get("reasoning_effort"), kw.get("max_completion_tokens")))
            outcome = outcomes[model].pop(0) if outcomes.get(model) else "ok"
            if isinstance(outcome, Exception):
                raise outcome
            return SimpleNamespace(model=model)
        return seen, SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def test_daily_cap_on_primary_switches_to_fallback_and_remembers(self):
        seen, client = self.fake({"openai/gpt-oss-120b": [_rate_limit(493)]})
        with patch.object(llm, "client", client), patch.object(llm, "MODEL", "openai/gpt-oss-120b"), \
                patch.object(llm, "FALLBACK_MODELS", ["openai/gpt-oss-20b"]):
            self.assertEqual(llm.chat([], []).model, "openai/gpt-oss-20b")
            self.assertEqual(llm.chat([], []).model, "openai/gpt-oss-20b")     # primary is skipped while cooling
        self.assertEqual([m for m, *_ in seen], ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "openai/gpt-oss-20b"])
        self.assertEqual(seen[0][1:], (llm.REASONING_EFFORT, llm.MAX_OUTPUT_TOKENS))

    def test_every_model_exhausted_raises_with_soonest_retry(self):
        seen, client = self.fake({"a": [_rate_limit(600)], "b": [_rate_limit(120)]})
        with patch.object(llm, "client", client), patch.object(llm, "MODEL", "a"), patch.object(llm, "FALLBACK_MODELS", ["b"]):
            with self.assertRaises(ProviderUnavailable) as ctx:
                llm.chat([], [])
            self.assertLessEqual(ctx.exception.retry_after, 121)
            with self.assertRaises(ProviderUnavailable):
                llm.chat([], [])
        self.assertEqual(len(seen), 2)                                    # no extra calls while both cool down
        self.assertEqual(seen[0][1], None)                                # non gpt-oss models get no extra params

    def test_short_wait_on_last_model_is_left_to_the_orchestrator_retry(self):
        seen, client = self.fake({"a": [_rate_limit(3)]})
        with patch.object(llm, "client", client), patch.object(llm, "MODEL", "a"), patch.object(llm, "FALLBACK_MODELS", []):
            with self.assertRaises(RateLimitError):
                llm.chat([], [])


class CategoryTests(TestCase):
    def test_model_category_words_map_to_catalog_categories(self):
        from purchases.consensus import canonical_category
        for word, expected in [("gift", "gift_bundle"), ("Birthday gifts", "gift_bundle"), ("Cabin rental", "rental"),
                               ("couches", "furniture"), ("ski passes", "passes"), ("telescope", "telescope")]:
            self.assertEqual(canonical_category(word), expected, word)


class GiftCategoryJourneyTests(PrivateJourneyTests):
    """The live model saved category 'gift'; the catalog calls it gift_bundle. Both must still find the offer."""

    def test_trip_then_topic_change_to_gift_never_reuses_stale_choices(self):
        pass

    def test_model_says_gift_and_still_finds_catalog_offer(self):
        self.send("birthday gift for my sister, just me, $40",
                  self.brief(goal="Birthday gift for sister", category="gift", group_size=1, budget_per_person=40),
                  self.search(category="gift", group_size=1, budget_per_person_max=40))
        option = Option.objects.get(group=self.group)
        self.assertEqual((option.category, option.title), ("gift_bundle", "Game night gift set"))
        out = self.send("1")
        self.assertEqual(TripPlan.objects.get().selected_option.title, "Game night gift set")

    def test_empty_search_keeps_a_good_model_question(self):
        out = self.send("telescope for me, $300",
                        self.brief(goal="Telescope", category="telescope", group_size=1, budget_per_person=300),
                        self.search(category="telescope", group_size=1, budget_per_person_max=300),
                        reply("Is this for planets or deep-sky viewing?"))
        self.assertEqual(out[-1].text_redacted, "Is this for planets or deep-sky viewing?")

    def test_empty_search_without_reply_offers_concrete_next_step(self):
        out = self.send("telescope for me, $300",
                        self.brief(goal="Telescope", category="telescope", group_size=1, budget_per_person=300),
                        self.search(category="telescope", group_size=1, budget_per_person_max=300), reply(""))
        text = out[-1].text_redacted
        self.assertIn("No catalog offer matched Telescope.", text)
        self.assertIn("Or tell me one or two must-haves", text)
