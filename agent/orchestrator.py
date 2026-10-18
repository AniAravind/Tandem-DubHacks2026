"""Grok tool-calling loop. Grok proposes tool calls; the rules engine (or the fake) decides what actually happens.

run(group_id, inbound) -> list[AgentMessage]. Nothing here imports payments/ and nothing here moves money.
"""
import json
import logging
import os
import pathlib
import re
import time

from django.conf import settings
from openai import BadRequestError, RateLimitError

from agent import cards, demo_script, safety
from agent.context import build_messages, build_group_state
from agent.llm import ProviderUnavailable
from agent.validate import validate
from chat.adapters.base import AgentMessage
from core.events import EventType, log_event
from chat.models import ChatMessage
from core.models import Group
from purchases.models import Option, Purchase, Vote

log = logging.getLogger(__name__)

TOOLS = json.loads((pathlib.Path(__file__).parent / "tools.json").read_text())
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
SCHEMAS = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}
SYSTEM_PROMPT = (pathlib.Path(__file__).parent / "prompts" / "system.md").read_text()

MAX_TOOL_CALLS = 4
MAX_REPAIRS = 2
PRIVATE_TOOLS = {"search_options", "publish_plan", "save_buying_brief", "publish_buying_plan", "live_search",
                 "compare_options", "remember_preference"}
MONEY_ADJACENT = {"create_purchase", "propose_fallback", "add_expense"}
FAILURE_TEXT = "I'm having trouble thinking right now. Your holds and votes are safe. Try again in a moment."
_LINK_CLAIM = re.compile(r"(?:here[’']?s|here is|(?:i(?:[’']ve| have)?|we(?:[’']ve| have)?) (?:sent|created|generated))[^.!?\n]{0,100}\b(?:link|invite)\b|\b(?:link|invite)\b[^.!?\n]{0,50}\b(?:ready|sent|created|generated)\b", re.I)


def get_engine(group_id=None):
    """The real RulesEngine once it is on main, else the fake (same execute(tool, args, actor) interface)."""
    try:
        from purchases.rules import RulesEngine
    except ImportError:
        from agent.fakes import FakeRulesEngine
        return FakeRulesEngine(group_id)
    return RulesEngine()


def _ids_in_args(tool: str, args: dict) -> tuple[set[str], set[str]]:
    """(member ids, other object ids) that a tool call refers to."""
    members, objects = set(args.get("member_ids") or []), set(args.get("option_ids") or [])
    if args.get("payer_id"):
        members.add(args["payer_id"])
    split = args.get("split") or {}
    members |= set(split.get("member_ids") or []) | set((split.get("custom_amounts") or {}).keys())
    for key in ("option_id", "purchase_id"):
        if args.get(key):
            objects.add(args[key])
    return members, objects


class Orchestrator:
    def __init__(self, group_id, engine=None, llm_chat=None):
        self.group = Group.objects.get(pk=group_id)
        self.engine = engine or get_engine(group_id)
        if llm_chat is None:
            from agent.llm import chat as llm_chat
        self.llm_chat = llm_chat
        self.tool_results: list[tuple[str, dict]] = []     # successful (tool, data) this turn, for cards + output filter
        self.tool_attempts = 0
        self.link_request_actor = None
        self.provider_retry_after = None
        self.provider_invalid_tools = False
        self.discovery_error = None
        self.succeeded: set[tuple[str, str]] = set()

    def no_match_text(self):
        """An empty catalog search still moves the conversation forward with one concrete next step."""
        from agent.context import missing_details
        from chat.models import PhotonSession
        from purchases import live
        session = PhotonSession.objects.filter(member__group=self.group).first()
        brief = session.planning_state if session and session.planning_state.get('goal') else {}
        lines = ["No catalog offer matched" + (f" {brief['goal']}." if brief else " that search.")]
        if live.configured():
            lines.append("Say “search the web” and I’ll pull real listings to compare.")
        missing = [m for m in missing_details(brief) if m != "budget per buyer"] if brief else []
        if missing:
            lines.append(f"Or tell me {missing[0]} and I’ll tailor ideas.")
        lines.append("Say “send me the link” for a planning page with suggestions. No payment can open without a quote.")
        return "\n".join(lines)

    def unavailable_text(self):
        """Honest provider-outage reply that names only the shortcuts that work right now without the AI."""
        from chat.models import PhotonSession
        from agent.private_actions import current_options, current_live_offers
        session = PhotonSession.objects.filter(member__group=self.group).first()
        wait = self.provider_retry_after
        when = (f" for about {max(1, round(wait / 60))} min" if wait and wait >= 90 else "")
        lines = [f"My AI provider is busy{when}, so I can’t read new requests yet. Your message is saved and no payment changed."]
        if self.provider_invalid_tools:
            return "I hit a snag while working that out. I still have your request, and no payment changed. Try again in a moment."
        options = current_options(self.group, session) if session else []
        if options:
            lines.append("You can still reply " + ", ".join(str(o["rank"]) for o in options) + " to pick from your shortlist.")
        elif session and current_live_offers(self.group, session):
            lines.append("You can still reply with a letter from the web results to start a planning page.")
        elif session and session.planning_state.get('goal'):
            lines.append('Your saved planning details are kept. I’ll need the AI provider to resume research.')
        if session and session.planning_state.get('goal'):
            lines.append("Say “send me the link” for your plan page.")
        return "\n".join(lines)

    def publish_requested_proposal(self, *, reuse_published=False):
        """Complete an explicit link request from real options, without invented IDs or members."""
        if not self.link_request_actor or self.tool_attempts >= MAX_TOOL_CALLS or any(name in {'publish_plan','publish_buying_plan'} for name, data in self.tool_results):
            return False
        from purchases.serializers import latest_options_json
        from purchases.models import TripPlan
        from chat.models import PhotonSession
        session = PhotonSession.objects.filter(member__group=self.group).first()
        brief = session.planning_state if session and session.planning_state.get('goal') else {}
        options = latest_options_json(self.group)['options']
        if brief:
            options = [o for o in options if o['meta'].get('brief_revision') == brief.get('revision')]
            if not options:
                return bool(self.run_tool('publish_buying_plan','{}',self.link_request_actor).get('ok'))
        existing = TripPlan.objects.filter(source_group=self.group).select_related('selected_option').order_by('-created_at').first() if reuse_published else None
        newest = Option.objects.filter(group=self.group).order_by('-created_at').first()
        if existing and existing.selected_option_id and newest and newest.created_at <= existing.created_at:
            source = Option.objects.filter(group=self.group, pk=existing.selected_option.meta.get('source_option_id')).first()
            if source:
                result = self.run_tool('publish_plan', json.dumps({'option_id':str(source.id),'title':existing.title,'summary':existing.summary}), self.link_request_actor)
                return bool(result.get('ok'))
        if not options:
            return False
        option = options[0]
        meta = option['meta']
        location = meta.get('location') or option['title']
        dates = ' to '.join(filter(None, [meta.get('check_in'), meta.get('check_out')]))
        summary = (f"Proposed purchase for {option['group_size']} buyer(s){(' · ' + dates) if dates else ''}. "
                   f"Suggested option: {option['title']}. This is the top-ranked match from your search, "
                   "for everyone to review together. Joining accepts this proposed plan; each person separately approves their own exact payment share.")
        result = self.run_tool('publish_plan', json.dumps({'option_id':option['id'], 'title':f'{location} · group plan'[:120], 'summary':summary}), self.link_request_actor)
        return bool(result.get('ok'))

    def private_reply_problem(self, text):
        from chat.models import PhotonSession
        if not PhotonSession.objects.filter(member__group=self.group).exists():
            return None
        session = PhotonSession.objects.get(member__group=self.group)
        brief = session.planning_state
        from purchases.briefs import explicit_buyer_count
        latest = ChatMessage.objects.filter(group=self.group,direction='in').order_by('-seq').values_list('text_redacted',flat=True).first() or ''
        if not brief.get('goal') and (explicit_buyer_count(latest) is not None or safety.planning_amounts_in(latest)):
            return 'Save the substantive buying request with save_buying_brief before asking a follow-up. Preserve the user-provided traveler count and budget. Then ask only the next missing detail.'
        from agent.dialogue import question_type
        if '?' in text:
            redundant_budget = (brief.get('budget_per_person_cents') is not None
                and re.search(r'confirm[^?]{0,100}budget|budget[^?]{0,100}confirm', text, re.I)
                and not re.search(r'include|cover|flight.only|hotel|total versus|per.person or total', text, re.I))
            if redundant_budget:
                return 'The budget is already explicitly confirmed in current_buying_brief. Do not ask to confirm it again. Apply the latest answer to conversation_context and call the relevant search/save tool, or ask only a different genuinely missing detail.'
            if question_type(text) == 'count' and brief.get('_buyer_count_confirmed'):
                return 'The total buyer count is already confirmed in current_buying_brief. Preserve it and move to the next genuinely missing detail or execute the relevant search tool.'
        if (_LINK_CLAIM.search(text) or re.search(r'https?://\S+/p/', text)) and not any(name in {'publish_plan','publish_buying_plan'} for name, data in self.tool_results):
            return 'No plan link has been created. Search actual options if needed, then call publish_plan. Never claim a link exists without successful publication.'
        if (re.search(r'search', text, re.I) and re.search(r'only you|only (?:one|1)|(?:one|1)[ -]?(?:active )?member', text, re.I)
                and re.search(r'can[’\']?t|cannot|add (?:more|the other)', text, re.I)):
            return 'One private member is expected. Search for the user’s intended group size now; joining happens on the website after publication.'
        _, reason = self.check_output(text)
        if reason:
            return ('Your last reply failed output validation ('+reason+'). Do not invent prices or claim payments happened. '
                    'Use exact source URLs and prices from current tool results, including live_search offers, or user-stated budgets. '
                    'Remove unsupported claims while still answering the request. If search results are irrelevant, refine the research with available tools; '
                    'if useful options cannot be verified, explain that plainly. Do not replace an answer with an unnecessary follow-up question.')
        return None

    # ------------------------------------------------------------------ id ownership (R1, re-checked by the engine)
    def _foreign_ids(self, tool: str, args: dict) -> list[str]:
        members, objects = _ids_in_args(tool, args)
        mine = {str(i) for i in self.group.members.filter(status="active").values_list("id", flat=True)}
        bad = [m for m in members if m not in mine]
        if objects:
            known = set()
            for model in (Option, Purchase, Vote):
                known |= {str(i) for i in model.objects.filter(group=self.group).values_list("id", flat=True)}
            if hasattr(self.engine, "known_ids"):
                known |= self.engine.known_ids()
            bad += [o for o in objects if o not in known]
        return bad

    def _reject(self, tool: str, reason: str, details=None) -> dict:
        log_event(EventType.AGENT_TOOL_REJECTED, actor="agent", group_id=self.group.id,
                  payload={"tool": tool[:64], "reason": reason})
        return {"ok": False, "error": {"code": reason, "message": details or reason}}

    def run_tool(self, name: str, raw_args: str, actor: str) -> dict:
        self.tool_attempts += 1
        if name not in TOOL_NAMES:
            return self._reject(name, "unknown_tool", "Only the listed tools exist.")
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return self._reject(name, "schema_invalid", "Arguments were not valid JSON.")
        problems = validate(args, SCHEMAS[name])
        if problems:
            return self._reject(name, "schema_invalid", "; ".join(problems[:5]))
        foreign = self._foreign_ids(name, args)
        if foreign:
            return self._reject(name, "unknown_id", "Use only ids from group_state or earlier tool results.")
        signature = (name, json.dumps(args, sort_keys=True))
        if signature in self.succeeded:
            # Models sometimes repeat a finished step (save the brief, save it again...). Never re-run it.
            return self._reject(name, "duplicate_call", "That exact call already succeeded this turn. Reply to the user now.")
        result = self.engine.execute(name, args, actor)
        if name == 'live_search' and not result.get('ok'):
            self.discovery_error = result.get('error')
        if result.get("ok"):
            self.succeeded.add(signature)
        log_event(EventType.AGENT_TOOL_CALLED if hasattr(self.engine, "snapshot") else "agent.tool_result", actor="agent", group_id=self.group.id,
                  payload={"tool": name, "ok": bool(result.get("ok")),
                           **({"code": result["error"].get("code")} if not result.get("ok") else {})})
        if result.get("ok"):
            data = result.get("data") or {}
            if name == 'save_buying_brief':
                # Models can search before saving the brief. Adopt only matching
                # quotes executed THIS turn, never older persisted suggestions.
                for tool, batch in self.tool_results:
                    if tool == 'live_search' and batch.get('search_id'):
                        from purchases.models import LiveOffer
                        LiveOffer.objects.filter(group=self.group, search_id=batch['search_id']).update(brief_revision=data['revision'])
                        continue
                    if tool != 'search_options':
                        continue
                    for option in batch.get('options', []):
                        record = Option.objects.filter(pk=option['id'], group=self.group).first()
                        if (record and record.category == data['category']
                                and option.get('group_size') == data['group_size']
                                and (data.get('budget_per_person_cents') is None
                                     or option['per_person_cents'] <= data['budget_per_person_cents'])):
                            record.meta = {**record.meta, 'brief_revision':data['revision']}
                            record.save(update_fields=['meta'])
                            option.setdefault('meta', {})['brief_revision'] = data['revision']
            self.tool_results.append((name, data))
        return result

    # ------------------------------------------------------------------ LLM loop
    def _call(self, messages: list[dict], **kw):
        self.provider_invalid_tools = False
        # Each decision sees the latest saved requirements and research, rather
        # than the stale state captured before the first tool call in this turn.
        messages = [{**message, 'content':'group_state: ' + json.dumps(build_group_state(self.group, self.engine,
                        json.loads(message['content'].removeprefix('group_state: ')).get('event')))}
                    if message.get('role') == 'system' and message.get('content','').startswith('group_state: ')
                    else message for message in messages]
        from chat.models import PhotonSession
        tools = TOOLS
        if PhotonSession.objects.filter(member__group=self.group).exists():
            tools = [tool for tool in TOOLS if tool['function']['name'] in PRIVATE_TOOLS]
        else:
            tools = [tool for tool in TOOLS if tool['function']['name'] != 'remember_preference']
        for attempt in range(2):                                    # one retry on timeout / API error
            try:
                return self.llm_chat(messages, tools, **kw)
            except ProviderUnavailable as exc:
                # Every model is cooling down (often a daily token cap). Retrying now only burns quota.
                self.provider_retry_after = exc.retry_after
                return None
            except RateLimitError as exc:
                log.warning('AI provider rate limit (attempt %s)', attempt + 1)
                if attempt == 0:
                    try:
                        delay = float(exc.response.headers.get('retry-after', '2'))
                    except (TypeError, ValueError):
                        delay = 2
                    if not 0 <= delay <= 15:
                        return None
                    time.sleep(max(1, delay) + 0.1)
            except BadRequestError as exc:
                if exc.code == "tool_use_failed":
                    self.provider_invalid_tools = True
                    # Provider validation occurs before a tool reaches our loop.
                    # Give the next attempt corrective context; never execute or
                    # echo the provider's untrusted failed_generation payload.
                    log.warning("Provider rejected generated tool arguments (attempt %s)", attempt + 1)
                    if attempt == 0:
                        messages.append({"role": "system", "content": (
                            "Your previous generated tool call was rejected by provider schema validation. "
                            "No tool from that response executed. Regenerate using the exact provided schema. "
                            "search_options.budget_per_person_max is USD dollars: $300 means 300, not 30000. "
                            "Preserve the user's intended budget; do not clamp or increase it. "
                            "Use group_state.current_date for relative dates. If uncertain, ask for clarification.")})
                else:
                    log.warning("Provider rejected AI request (attempt %s, HTTP 400)", attempt + 1)
            except Exception:                                       # noqa: BLE001
                log.warning("xAI call failed (attempt %s)", attempt + 1, exc_info=True)
        return None

    def run_scripted(self, actor: str, event: dict | None, text: str) -> list[AgentMessage] | None:
        reply_text = demo_script.scripted_text(self, actor, text, event)
        return self.compose(reply_text) if reply_text else None

    def run(self, actor: str, event: dict | None = None, text: str = "") -> list[AgentMessage]:
        from chat.models import PhotonSession
        private = PhotonSession.objects.filter(member__group=self.group).exists()
        if os.getenv("DEMO_SCRIPTED") == "1" and not private:
            return self.run_scripted(actor, event, text) or [AgentMessage(text=safety.SAFE_TEXT_NO_TOOLS)]
        if private and text:
            from agent.private_actions import handle
            direct = handle(self, actor, text)
            if direct is not None:
                return direct
        messages = build_messages(self.group, SYSTEM_PROMPT, self.engine, event)
        from chat.models import PhotonSession
        short_link_request = bool(re.fullmatch(
            r'\s*(?:please\s+)?(?:add|invite|share|link|send it|create it|make it|'
            r'(?:send|resend|give|create|make|generate|share)(?:\s+(?:me|us))?(?:\s+(?:a|the|my|our))?'
            r'(?:\s+(?:private|organizer|group|plan|invite|shared|trip))*\s+link)(?:\s+please)?[.!?]?\s*', text, re.I))
        explicit_link_request = short_link_request or bool(re.search(r'\b(?:create|make|generate|send|share|give|want|need|where)[^.!?\n]{0,80}\b(?:link|invite)\b|\bpublish (?:it|the|a|my|this|our)\b', text, re.I))
        if explicit_link_request and PhotonSession.objects.filter(member__group=self.group).exists():
            self.link_request_actor = actor
            if short_link_request and self.publish_requested_proposal(reuse_published=True):
                return self.compose(None)
        calls = repairs = 0
        original_text = text
        text = None
        while True:
            force_text = calls >= MAX_TOOL_CALLS or repairs >= MAX_REPAIRS
            resp = self._call(messages, **({"tool_choice": "none"} if force_text else {}))
            if resp is None:
                if self.tool_results:
                    # State changes already happened. Deliver their real cards
                    # and private plan links even if narration is unavailable.
                    completed = self.compose(None)
                    if completed:
                        return completed
                if settings.DEMO_MODE and not self.tool_results and not private:     # Grok unreachable: the demo script is the backup
                    scripted = self.run_scripted(actor, event, original_text)
                    if scripted:
                        return scripted
                return [AgentMessage(text=self.unavailable_text() if private else FAILURE_TEXT)]
            msg = resp.choices[0].message
            if not msg.tool_calls or force_text:
                text = (msg.content or "").strip()
                problem = self.private_reply_problem(text)
                if problem:
                    if repairs < MAX_REPAIRS and not force_text:
                        repairs += 1
                        messages.append({'role':'assistant','content':text})
                        messages.append({'role':'system','content':problem})
                        continue
                    text = self.private_next_step()
                break
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [{"id": tc.id, "type": "function",
                                             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                            for tc in msg.tool_calls]})
            ran_money_tool = False
            round_start = len(self.tool_results)
            for tc in msg.tool_calls:
                if calls >= MAX_TOOL_CALLS:
                    result = {"ok": False, "error": {"code": "too_many_tool_calls", "message": "Stop calling tools and reply."}}
                elif ran_money_tool and tc.function.name in MONEY_ADJACENT:
                    result = {"ok": False, "error": {"code": "one_at_a_time", "message": "Call one money-related tool at a time."}}
                else:
                    calls += 1
                    result = self.run_tool(tc.function.name, tc.function.arguments, actor)
                    ran_money_tool |= tc.function.name in MONEY_ADJACENT
                    if result.get("error", {}).get("code") in {"schema_invalid", "duplicate_call"}:
                        repairs += 1
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
            this_round = self.tool_results[round_start:]
            if private and self.discovery_error and self.discovery_error.get('code') in {'live_search_unavailable','live_search_quota','rate_limited'} and not any(name == 'live_search' for name, _ in self.tool_results):
                return [AgentMessage(text='I couldn’t retrieve real listings for that request. ' + self.discovery_error.get('message','Please try again shortly.'))]
            if private and any(name == 'live_search' for name, _ in this_round) and calls < MAX_TOOL_CALLS:
                from chat.models import PhotonSession
                from purchases import live
                brief = PhotonSession.objects.get(member__group=self.group).planning_state
                done = {o.kind for o in live.for_plan(self.group, brief.get('revision'))}
                pending = set(brief.get('research_kinds', [])) - done
                if pending and not brief.get('open_questions'):
                    messages.append({'role':'system','content':'Research components requested in the saved brief still need tool calls: '+', '.join(sorted(pending))+'. Search only those relevant components; do not repeat a completed search.'})
                    continue
            # Tool results return to the model so it can decide whether to refine,
            # capture requirements, compare, publish, clarify, or finish. Successful
            # searches do not terminate the agent's reasoning automatically.
        return self.compose(text)

    def check_output(self, text):
        tool_cents = set().union(*(safety.cents_in(d) for _, d in self.tool_results)) if self.tool_results else set()
        from chat.models import PhotonSession
        session = PhotonSession.objects.filter(member__group=self.group).first()
        parser = safety.planning_amounts_in if session else safety.amounts_in
        typed = set().union(*(parser(t) for t in ChatMessage.objects.filter(group=self.group, direction="in")
                              .order_by("-seq").values_list("text_redacted", flat=True)[:50]))
        if session:
            from purchases.serializers import latest_options_json
            for option in latest_options_json(self.group)['options']:
                if not session.planning_state or option['meta'].get('brief_revision') == session.planning_state.get('revision'):
                    tool_cents |= safety.cents_in({k:option[k] for k in ('total_cents','per_person_cents')})
        caps = {c for c in (m.budget_cap_cents for m in self.group.members.all()) if c is not None}
        out, reason = safety.filter_text(text, tool_cents, typed, caps)
        if reason is None:
            # A link must come from a tool result (live listings) or this app, never from the model's imagination.
            from purchases.models import LiveOffer
            known = {o.get("url") for _, d in self.tool_results for o in d.get("offers", [])}
            for url in safety.urls_in(text):
                own = url.startswith(settings.PUBLIC_BASE_URL.rstrip("/") + "/") if settings.PUBLIC_BASE_URL else False
                if not own and url not in known and not LiveOffer.objects.filter(group=self.group, url=url).exists():
                    return "", "unverified_link"
        return out, reason

    def private_next_step(self):
        from chat.models import PhotonSession
        session = PhotonSession.objects.filter(member__group=self.group).first()
        brief = session.planning_state if session and session.planning_state.get('goal') else {}
        if brief:
            if brief.get('budget_per_person_cents') is None:
                return f"For {brief['goal']} with {brief['group_size']} buyer(s), what budget should I use per buyer?"
            return f"I have {brief['goal']} for {brief['group_size']} buyer(s), with your {cards.usd(brief['budget_per_person_cents'])} per-buyer budget. I don’t have a verified offer yet; say ‘send me the link’ to open the planning page."
        return "I haven’t created a buying plan yet. What are you buying, how many buyers, and what budget should I use? One buyer is fine."

    def safe(self, text: str) -> str:
        out, reason = self.check_output(text)
        if reason:
            log_event(EventType.AGENT_TOOL_REJECTED, actor="agent", group_id=self.group.id,
                      payload={"tool": "output", "reason": reason})
            from chat.models import PhotonSession
            if PhotonSession.objects.filter(member__group=self.group).exists():
                return self.private_next_step()
            return safety.SAFE_TEXT if self.tool_results else safety.SAFE_TEXT_NO_TOOLS
        return out

    def compose(self, text: str | None) -> list[AgentMessage]:
        if self.link_request_actor and any(name in {'search_options','save_buying_brief'} for name, data in self.tool_results):
            self.publish_requested_proposal()
        presenting = [(name, data) for name, data in self.tool_results
                      if name in {'search_options', 'live_search', 'compare_options'}]
        if any(name in {'publish_plan','publish_buying_plan'} for name, data in self.tool_results) and not any(name == 'live_search' for name, data in presenting):
            # Sharing instructions and organizer credentials belong to the
            # server-generated private attachment, never model narration.
            text = None
        elif presenting and presenting[-1][0] == 'compare_options':
            text = cards.compare_text(presenting[-1][1])
        elif presenting and presenting[-1][0] == 'live_search':
            batches = [data for name,data in presenting if name == 'live_search']
            # Render retrieved titles, prices and URLs directly: free-form model
            # narration can attach a real price or URL to an invented product.
            text = '\n\n'.join((data.get('query','Research')+'\n\n' if len(batches)>1 else '')+cards.live_offers_text(data) for data in batches)
            if len(batches)>1:
                text += '\n\nTo choose from an earlier section, tell me the type and title, such as the hotel name. A letter alone selects from the latest search.'
        elif any(name == 'search_options' for name, data in self.tool_results):
            from chat.models import PhotonSession
            if PhotonSession.objects.filter(member__group=self.group).exists():
                found = next(data.get('options', []) for name, data in reversed(self.tool_results) if name == 'search_options')
                text = ('🔎 Your shortlist\n\nA few picks to consider. Reply with a number or name and I’ll make your plan link.\n\nReview the plan before approving your share.' if found
                        else text if text and not self.private_reply_problem(text) else self.no_match_text())
        elif text and self.private_reply_problem(text):
            text = self.private_next_step()
        text = self.safe(text) if text else text
        labels = {o["id"]: o["title"] for name, d in self.tool_results if name == "search_options" for o in d.get("options", [])}
        presentation = []
        for name, data in self.tool_results:
            d = dict(data)
            if name == "create_purchase" and not hasattr(self.engine, "snapshot"):
                purchase = Purchase.objects.filter(pk=d.get("purchase_id"), group=self.group).select_related("option").first()
                if purchase:
                    d.update(title=purchase.option.title, total_cents=purchase.total_cents,
                             shares=[{"member_id": str(sh.member_id), "display_name": sh.member.display_name,
                                      "status": sh.status, "amount_cents": sh.amount_cents}
                                     for sh in purchase.shares.select_related("member").all()])
            if name == "add_expense" and not hasattr(self.engine, "snapshot"):
                # The real ledger already posts this expense's ExpenseCard; a second copy here duplicated it.
                continue
            if name == "start_vote" and not hasattr(self.engine, "snapshot"):
                vote = Vote.objects.filter(pk=d.get("vote_id"), group=self.group).first()
                if vote:
                    choices = []
                    for choice in vote.choices:
                        option = Option.objects.filter(pk=choice.get("option_id"), group=self.group).first()
                        choices.append({**choice, "per_person_cents": (option.meta or {}).get("per_person_cents", 0) if option else 0})
                    d["choices"] = choices
            presentation.append((name, d))
        attached = [m for name, d in presentation for m in cards.cards_for(name, d, labels, self.group.timezone)]
        for name, data in self.tool_results:
            if name in {'publish_plan','publish_buying_plan'}:
                from purchases.models import TripPlan
                from purchases.plans import owner_url
                plan = TripPlan.objects.filter(pk=data.get('plan_id'), source_group=self.group).first()
                if plan:
                    featured = next((o for o in plan.details.get('live_offers') or [] if o['offer_id'] == plan.details.get('featured_offer_id')), None) or next((o for o in plan.details.get('live_offers') or [] if o['label'] == plan.details.get('featured_offer')), None)
                    if plan.selected_option_id:
                        note = 'Each person reviews their own share on the plan.'
                    elif featured:
                        note = (f"It features {featured['title']} from {featured['source']}. That’s a live web listing, "
                                "so it’s for planning together: Tandem can’t take payment for it.")
                    else:
                        note = 'This is planning-only until a supported merchant offer is attached.'
                    attached.append(AgentMessage(text=f'Here’s your plan for {plan.title}.\n\nAdd your name and number through your private link below, then invite the others from the page.\n\n'+note,
                        link=owner_url(plan), private_to_member_id=str(self.group.organizer_id)))
        out = [AgentMessage(text=text)] if text else []
        if not out and not attached and any(name=='save_buying_brief' for name,data in self.tool_results):
            out = [AgentMessage(text=self.private_next_step())]
        public = [m for m in attached if not m.private_to_member_id]
        if out and len(public) == 1:                                # a single card rides on the text message
            out[0].card = public[0].card
            attached = [m for m in attached if m is not public[0]]
        return out + attached


def latest_text(group_id, member_id) -> str:
    m = ChatMessage.objects.filter(group_id=group_id, member_id=member_id, direction="in").order_by("-seq").first()
    return m.text_redacted if m else ""


def run(group_id, inbound, event: dict | None = None, **kw) -> list[AgentMessage]:
    """Entry point used by chat.services. inbound is an InboundMessage (or None for server events)."""
    actor = f"member:{inbound.member_id}" if inbound else "agent"
    text = (inbound.text or latest_text(group_id, inbound.member_id)) if inbound else ""
    refusal = safety.guard(text) if text else None
    if refusal:
        log_event(EventType.AGENT_TOOL_REJECTED, actor=actor, group_id=group_id, payload={"tool": "guard", "reason": "injection"})
        return [AgentMessage(text=refusal)]
    return Orchestrator(group_id, **kw).run(actor, event, text)
