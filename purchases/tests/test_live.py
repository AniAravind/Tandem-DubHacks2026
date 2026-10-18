import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from agent.context import build_group_state
from agent.orchestrator import Orchestrator
from agent.tests.helpers import reply, seed, tool_call
from chat.models import ChatMessage, PhotonSession
from core.models import Group, Member
from purchases import live
from purchases.models import LiveOffer, Purchase, TripPlan
from purchases.rules import RulesEngine

ROWS = [
    {"title": "Blue Bear Cabin | Hot tub", "url": "https://www.vrbo.com/2023227312345678ha",
     "content": "Sleeps 6. From $335/night. **Pet friendly**, EV charger."},
    {"title": "Little Bear Cabin 536", "url": "https://www.findrentals.com/branchvr/p175066",
     "content": "3 bedrooms near Heavenly. $295 per night, 2-night minimum."},
    {"title": "Not secure", "url": "http://example.com/cabin", "content": "$100/night"},
    {"title": "Tahoe cabins guide", "url": "https://www.example.org/guide", "content": "Call 555-123-4567 for deals."},
]


class FakeTavily:
    def __init__(self, rows=ROWS, status=200):
        self.rows, self.status, self.calls = rows, status, []

    def post(self, url, **kw):
        self.calls.append(kw["json"])
        return SimpleNamespace(status_code=self.status, json=lambda: {"results": self.rows})


class NoLLM:
    def __call__(self, *a, **kw):
        raise AssertionError("This turn must not call the AI provider")


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "live-tests"}})
@patch.dict("os.environ", {"TAVILY_API_KEY": "tvly-test"})
class LiveSearchTests(TestCase):
    def setUp(self):
        cache.clear()
        seed()
        self.group = Group.objects.create(name="Private planning", channel="imessage")
        self.member = Member.objects.create(group=self.group, display_name="Planner", role="organizer",
                                            phone_e164_hash=hashlib.sha256(b"p").hexdigest())
        self.group.organizer = self.member
        self.group.save()
        self.session = PhotonSession.objects.create(key="live-test", member=self.member)
        self.actor = f"member:{self.member.id}"
        self.engine = RulesEngine()
        self.say("Tahoe cabin Nov 14-16 for me and 5 friends, $200 each")
        result = self.engine.execute("save_buying_brief", {"goal": "Tahoe cabin weekend", "category": "rental", "group_size": 6,
                                                           "budget_per_person": 200, "requirements": ["Nov 14-16", "hot tub"]}, self.actor)
        self.assertTrue(result["ok"], result)
        self.fake = FakeTavily()
        patcher = patch("purchases.live.httpx", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def say(self, text):
        seq = (ChatMessage.objects.filter(group=self.group).order_by("-seq").values_list("seq", flat=True).first() or 0) + 1
        return ChatMessage.objects.create(group=self.group, member=self.member, direction="in", seq=seq, text_redacted=text)

    def search(self, **extra):
        args = {"query": "South Lake Tahoe cabin rental", "kind": "stay", "group_size": 6, "budget_per_person_max": 200,
                "dates": {"start": "2026-11-14", "end": "2026-11-16"}, **extra}
        return self.engine.execute("live_search", args, self.actor)

    def test_prices_only_from_source_text_and_unsafe_rows_dropped(self):
        result = self.search()
        self.assertTrue(result["ok"], result)
        offers = result["data"]["offers"]
        self.assertEqual([o["source"] for o in offers], ["vrbo.com", "findrentals.com", "example.org"])
        blue, little, guide = offers
        self.assertEqual(blue["title"], "Blue Bear Cabin · Hot tub")
        self.assertEqual((little["price_cents"], little["price_unit"], little["price_text"]), (29500, "night", "$295 per night"))
        self.assertEqual(little["est_per_person_cents"], 9834)          # 2 nights / 6 people, rounded up
        self.assertTrue(little["fits_budget"])
        self.assertEqual(guide["label"], "C")                          # fits-budget results are listed first
        self.assertIsNone(guide["price_cents"])
        self.assertNotIn("555-123-4567", guide["snippet"])
        self.assertNotIn("**", blue["snippet"])
        self.assertTrue(all(o["checkout_ready"] is False and o["live"] for o in offers))
        self.assertEqual(LiveOffer.objects.filter(group=self.group).count(), 3)

    def test_unconfigured_search_is_honest_and_stores_nothing(self):
        with patch.dict("os.environ", {"TAVILY_API_KEY": ""}):
            result = self.search()
            self.assertFalse(build_group_state(self.group)["live_search_available"])
        self.assertEqual(result["error"]["code"], "live_search_unavailable")
        self.assertFalse(LiveOffer.objects.exists())

    def test_cache_and_quota_guard_protect_provider_allowance(self):
        self.assertTrue(self.search()["ok"])
        self.assertTrue(self.search()["ok"])
        self.assertEqual(len(self.fake.calls), 1)                      # identical query served from cache
        for _ in range(3):
            self.search(query="Tahoe cabin different query " + _ * "x")
        self.assertEqual(self.search(query="one more")["error"]["code"], "rate_limited")
        cache.clear()
        with patch("purchases.live.DAILY_MAX", 0):
            self.assertEqual(self.search()["error"]["code"], "live_search_quota")

    def test_provider_errors_become_clear_tool_errors(self):
        self.fake.status = 432
        self.assertEqual(self.search()["error"]["code"], "live_search_quota")
        self.fake.status = 500
        self.assertEqual(self.search(query="other")["error"]["code"], "live_search_unavailable")

    def test_live_turn_returns_results_to_model_before_finishing(self):
        calls = []
        def llm(messages, tools, **kw):
            calls.append(1)
            if len(calls) > 1:
                return reply('Here are the listings.')
            return reply(calls=[tool_call("live_search", {"query": "South Lake Tahoe cabin rental", "kind": "stay", "group_size": 6,
                                                           "dates": {"start": "2026-11-14", "end": "2026-11-16"}})])
        self.say("show me real cabins on the web")
        out = Orchestrator(self.group.id, llm_chat=llm).run(self.actor, text="show me real cabins on the web")
        self.assertEqual(len(calls), 2)
        self.assertIn("B. Little Bear Cabin 536", out[0].text)
        self.assertIn("C. Tahoe cabins guide\nprice not listed", out[0].text)
        self.assertIn("https://www.vrbo.com/2023227312345678ha", out[0].text)   # long listing ids are not card numbers
        self.assertIn("not Tandem quotes", out[0].text)
        self.assertEqual(out[0].card.type, "LiveOffersCard")

    def test_model_chosen_research_returns_source_links_for_varied_requests(self):
        for request in ("Give me ideas of what to buy", "Recommend options for a weekend trip",
                        "What could I get for my home office?"):
            with self.subTest(request=request):
                responses = iter([
                    reply(calls=[tool_call('live_search', {'query': 'Tahoe cabin rental', 'kind': 'stay', 'group_size': 6})]),
                ])
                def model(*args, **kwargs):
                    return next(responses, reply("Here are the listings."))
                self.say(request)
                out = Orchestrator(self.group.id, llm_chat=model).run(self.actor, text=request)
                self.assertIn('https://www.vrbo.com/2023227312345678ha', out[0].text)
                self.assertFalse(TripPlan.objects.filter(source_group=self.group).exists())

    def test_publication_does_not_hide_researched_links(self):
        result = self.search()
        self.assertTrue(result['ok'], result)
        orchestrator = Orchestrator(self.group.id)
        orchestrator.tool_results = [('live_search', result['data']), ('publish_buying_plan', {})]
        out = orchestrator.compose(None)
        self.assertIn('https://www.vrbo.com/2023227312345678ha', out[0].text)

    def test_model_controls_tool_sequence_and_combined_budget_research(self):
        self.fake.rows = [{'title':'Spider-Man graphic novel $24', 'url':'https://example.com/products/graphic-novel', 'content':'Paperback gift. $24 listed.'}]
        request = 'Gift ideas for my friend. I am splitting with another friend and have a combined budget of $60 dollars.'
        self.say(request)
        calls = []
        def model(messages, tools, **kwargs):
            calls.append(kwargs)
            if len(calls) > 2:
                return reply('Here are the listings.')
            if len(calls) == 1:
                return reply(calls=[tool_call('save_buying_brief', {'goal':'Birthday gift', 'category':'gift', 'group_size':2,
                    'budget_total':60, 'requirements':['Spider-Man graphic novel'], 'new_goal':True})])
            self.assertNotIn('tool_choice',kwargs)
            self.assertIn('publish_plan',[tool['function']['name'] for tool in tools])
            return reply(calls=[tool_call('live_search', {'query':'Spider-Man graphic novel gift under $60', 'kind':'gift',
                'group_size':2,'budget_per_person_max':30,'route':{}})])
        out = Orchestrator(self.group.id, llm_chat=model).run(self.actor, text=request)
        self.assertEqual(len(calls),3)
        self.assertIn('https://example.com/products/graphic-novel',out[0].text)
        self.assertFalse(TripPlan.objects.filter(source_group=self.group).exists())
        self.session.refresh_from_db()
        self.assertEqual(self.session.planning_state['budget_per_person_cents'],3000)

    def test_research_can_precede_brief_and_preserves_sourced_answer(self):
        self.say("Cabin weekend for 6 people, $200 each, with a hot tub")
        responses = iter([
            reply(calls=[tool_call('live_search', {'query':'Tahoe hot tub cabin', 'kind':'stay', 'group_size':6})]),
            reply(calls=[tool_call('save_buying_brief', {'goal':'Tahoe cabin weekend', 'category':'rental',
                'group_size':6, 'budget_per_person':200, 'requirements':['hot tub', 'quiet location']})]),
            reply('My pick is Blue Bear: the hot tub earns its keep. https://www.vrbo.com/2023227312345678ha'),
        ])
        out = Orchestrator(self.group.id, llm_chat=lambda *a, **kw: next(responses)).run(self.actor)
        self.assertIn('the hot tub earns its keep', out[0].text)
        self.session.refresh_from_db()
        self.assertTrue(LiveOffer.objects.filter(group=self.group,
            brief_revision=self.session.planning_state['revision']).exists())

    def test_failed_live_search_explains_outage_without_hallucinated_fallback(self):
        self.fake.status = 500
        self.say('Show me real options')
        def model(*args, **kwargs):
            return reply(calls=[tool_call('live_search', {'query':'Tahoe cabin', 'kind':'stay','group_size':6})])
        out = Orchestrator(self.group.id, llm_chat=model).run(self.actor,text='Show me real options')
        self.assertIn('couldn’t retrieve real listings',out[0].text)
        self.assertNotIn('send me the link',out[0].text)

    def test_letter_reply_features_offer_on_planning_page_without_payment(self):
        self.search()
        self.say("B")
        out = Orchestrator(self.group.id, llm_chat=NoLLM()).run(self.actor, text="B")
        link = next(m for m in out if m.link)
        self.assertIn("Little Bear Cabin 536", link.text)
        self.assertIn("can’t take payment", link.text)
        plan = TripPlan.objects.get(source_group=self.group)
        self.assertEqual(plan.details["featured_offer"], "B")
        self.assertEqual(len(plan.details["live_offers"]), 3)
        preview = APIClient().get(f"/api/v1/plans/{plan.id}").json()
        self.assertFalse(preview["checkout_ready"])
        self.assertEqual(preview["details"]["live_offers"][1]["url"], "https://www.findrentals.com/branchvr/p175066")
        self.assertFalse(Purchase.objects.exists())
        out = Orchestrator(self.group.id, llm_chat=NoLLM()).run(self.actor, text="E")
        self.assertIn("isn’t in the current web results", out[0].text)

    def test_compare_catalog_and_live_without_model_call(self):
        result = self.engine.execute("search_options", {"category": "rental", "group_size": 6, "budget_per_person_max": 200}, self.actor)
        self.assertTrue(result["data"]["options"])
        self.search()
        self.say("compare 1 and A")
        out = Orchestrator(self.group.id, llm_chat=NoLLM()).run(self.actor, text="compare 1 and A")
        text = out[0].text
        self.assertIn("Side by side", text)
        self.assertIn("demo offer, can be paid in Tandem", text)
        self.assertIn("A. Blue Bear Cabin · Hot tub · $335/night listed · about $111.67 each · vrbo.com, web only", text)
        self.assertIn("Within your $200 budget", text)
        self.assertEqual(out[0].card.type, "CompareCard")
        self.assertEqual(Orchestrator(self.group.id, llm_chat=NoLLM()).run(self.actor, text="compare 7 and A")[0].text,
                         "7 isn’t on your current shortlist.")

    def test_topic_change_hides_old_live_results(self):
        self.search()
        self.assertEqual(len(build_group_state(self.group)["live_results"]), 3)
        self.say("Actually a birthday gift for my sister, just me, $40")
        self.assertTrue(self.engine.execute("save_buying_brief", {"goal": "Gift for sister", "category": "gift", "group_size": 1,
                                                                  "budget_per_person": 40}, self.actor)["ok"])
        self.assertEqual(build_group_state(self.group)["live_results"], [])
        self.assertEqual(self.engine.execute("compare_options", {"offer_labels": ["A", "B"]}, self.actor)["error"]["code"], "not_found")

    def test_remembered_preferences_persist_across_goals_and_reject_private_data(self):
        ok = self.engine.execute("remember_preference", {"remember": ["vegetarian", "flies from SEA"]}, self.actor)
        self.assertEqual(ok["data"]["remembered"], ["vegetarian", "flies from SEA"])
        for bad in (["budget under $50"], ["call me at 206-555-0100"], ["https://evil.example"]):
            self.assertEqual(self.engine.execute("remember_preference", {"remember": bad}, self.actor)["error"]["code"], "not_a_preference")
        self.engine.execute("remember_preference", {"forget": ["flies from SEA"]}, self.actor)
        self.say("New topic: a couch just for me")
        self.assertTrue(self.engine.execute("save_buying_brief", {"goal": "New couch", "category": "furniture", "group_size": 1}, self.actor)["ok"])
        state = build_group_state(self.group)
        self.assertEqual(state["remembered_preferences"], ["vegetarian"])
        self.assertIn("budget per buyer", state["missing_details"])

    def test_model_invented_link_is_blocked(self):
        o = Orchestrator(self.group.id)
        self.assertEqual(o.check_output("Book it at https://totally-real-cabins.example/deal")[1], "unverified_link")
        self.search()
        self.assertIsNone(Orchestrator(self.group.id).check_output("See https://www.vrbo.com/2023227312345678ha")[1])

    def test_group_chat_live_search_posts_one_card_and_never_opens_payment(self):
        from agent.tests.helpers import TAHOE
        tahoe = Group.objects.get(pk=TAHOE)
        maya = tahoe.members.get(display_name="Maya")
        since = __import__("core.models", fromlist=["EventLog"]).EventLog.objects.order_by("-seq").values_list("seq", flat=True).first()
        def llm(messages, tools, **kw):
            self.assertNotIn("remember_preference", {t["function"]["name"] for t in tools})
            if any(m.get("role") == "tool" for m in messages):
                return reply("Here are some real cabins to look at.")
            return reply(calls=[tool_call("live_search", {"query": "Tahoe cabin", "kind": "stay", "group_size": 6})])
        out = Orchestrator(tahoe.id, llm_chat=llm).run(f"member:{maya.id}", text="@tandem any real cabins online?")
        self.assertEqual(len(out), 1)
        self.assertIn("Live picks from the web", out[0].text)
        self.assertEqual(out[0].card.type, "LiveOffersCard")
        self.assertTrue(all(o["fits_budget"] is None for o in out[0].card.data["offers"]))   # no private budgets in groups
        from purchases.testing import assert_no_money_state_change
        assert_no_money_state_change(tahoe.id, since)

    def test_full_trip_research_preserves_all_components_and_tool_requests(self):
        from agent.tests.helpers import ScriptedLLM
        args = {'goal':'Tahoe cabin weekend','category':'rental','group_size':6,
                'budget_per_person':200,'requirements':['Nov 14-16','hot tub'],
                'decision_details':[{'topic':'Origin','value':'Seattle'},{'topic':'Destination','value':'Tahoe'}],
                'research_kinds':['flight','stay','activity'],'open_questions':[],
                'plan_sections':[{'heading':'Weekend outline','body':'Suggestion: arrive Friday, hike Saturday, return Sunday.'}]}
        self.assertTrue(self.engine.execute('save_buying_brief',args,self.actor)['ok'])
        requests = [('flight','Seattle to Reno Nov 14-16 flights'),('stay','Tahoe hot tub cabin Nov 14-16'),('activity','Tahoe hiking November trails')]
        class ResearchModel:
            def __init__(inner): inner.requests=[]
            def __call__(inner,messages,tools,**kw):
                kind,query=requests[len(inner.requests)]
                inner.requests.append(messages)
                self.fake.rows = [{'title':'Seattle to Reno flights','url':'https://example.com/flights/seattle/reno','content':'Advertised fares from $400'}] if kind=='flight' else ROWS
                args={'query':query,'kind':kind,'group_size':6}
                if kind=='flight': args['route']={'origin':'Seattle','destination':'Reno'}
                return reply(calls=[tool_call('live_search',args)])
        model=ResearchModel()
        out = Orchestrator(self.group.id,llm_chat=model).run(self.actor,text='Find the flights, stay and things to do for our trip')
        self.assertEqual(len(model.requests),3)
        self.assertEqual({o.kind for o in live.for_plan(self.group,build_group_state(self.group)['current_buying_brief']['revision'])}, {'flight','stay','activity'})
        self.assertEqual(sum(m.card is not None and m.card.type=='LiveOffersCard' for m in out),3)
        published=self.engine.execute('publish_buying_plan',{},self.actor)
        self.assertTrue(published['ok'],published)
        plan=TripPlan.objects.get(pk=published['data']['plan_id'])
        self.assertEqual({o['kind'] for o in plan.details['live_offers']},{'flight','stay','activity'})
        self.assertEqual(plan.details['plan_sections'],args['plan_sections'])
        flight = next(o for o in live.for_plan(self.group,plan.details['revision']) if o.kind == 'flight')
        featured = self.engine.execute('publish_buying_plan', {'featured_offer_id':str(flight.id)}, self.actor)
        self.assertTrue(featured['ok'], featured)
        plan.refresh_from_db()
        self.assertEqual(plan.details['featured_offer_id'],str(flight.id))
        rejected = self.engine.execute('publish_buying_plan', {'featured_offer_id':'00000000-0000-0000-0000-000000000000'}, self.actor)
        self.assertFalse(rejected['ok'])
        self.assertEqual(build_group_state(self.group)['missing_details'],[])

    def test_specific_gift_questions_persist_without_a_scripted_menu(self):
        self.say("A birthday gift for myself")
        result=self.engine.execute('save_buying_brief',{'goal':'Birthday gift for a coffee-loving friend',
            'category':'gift','group_size':1,
            'decision_details':[{'topic':'Interests','value':'Coffee and cooking'}],
            'open_questions':['When does the gift need to arrive?'], 'research_kinds':['gift']},self.actor)
        self.assertTrue(result['ok'],result)
        self.assertEqual(build_group_state(self.group)['missing_details'],['When does the gift need to arrive?'])
        result=self.engine.execute('save_buying_brief',{'goal':'Gift','category':'gift','group_size':1,
            'plan_sections':[{'heading':'Buy','body':'https://invented.example/fake'}]},self.actor)
        self.assertFalse(result['ok'])
