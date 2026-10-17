"""Grok tool-calling loop. Grok proposes tool calls; the rules engine (or the fake) decides what actually happens.

run(group_id, inbound) -> list[AgentMessage]. Nothing here imports payments/ and nothing here moves money.
"""
import json
import logging
import pathlib

from agent import cards, safety
from agent.context import build_messages
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
MONEY_ADJACENT = {"create_purchase", "propose_fallback", "add_expense"}
FAILURE_TEXT = "I'm having trouble thinking right now. Your holds and votes are safe. Try again in a moment."


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
        if name not in TOOL_NAMES:
            return self._reject(name, "unknown_tool", "Only the 6 listed tools exist.")
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
        result = self.engine.execute(name, args, actor)
        log_event(EventType.AGENT_TOOL_CALLED, actor="agent", group_id=self.group.id,
                  payload={"tool": name, "ok": bool(result.get("ok")),
                           **({"code": result["error"].get("code")} if not result.get("ok") else {})})
        if result.get("ok"):
            self.tool_results.append((name, result.get("data") or {}))
        return result

    # ------------------------------------------------------------------ LLM loop
    def _call(self, messages: list[dict], **kw):
        for attempt in range(2):                                    # one retry on timeout / API error
            try:
                return self.llm_chat(messages, TOOLS, **kw)
            except Exception:                                       # noqa: BLE001
                log.warning("xAI call failed (attempt %s)", attempt + 1, exc_info=True)
        return None

    def run(self, actor: str, event: dict | None = None) -> list[AgentMessage]:
        messages = build_messages(self.group, SYSTEM_PROMPT, self.engine, event)
        calls = repairs = 0
        text = None
        while True:
            force_text = calls >= MAX_TOOL_CALLS or repairs > MAX_REPAIRS
            resp = self._call(messages, **({"tool_choice": "none"} if force_text else {}))
            if resp is None:
                return [AgentMessage(text=FAILURE_TEXT)]
            msg = resp.choices[0].message
            if not msg.tool_calls or force_text:
                text = (msg.content or "").strip()
                break
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [{"id": tc.id, "type": "function",
                                             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                            for tc in msg.tool_calls]})
            ran_money_tool = False
            for tc in msg.tool_calls:
                if calls >= MAX_TOOL_CALLS:
                    result = {"ok": False, "error": {"code": "too_many_tool_calls", "message": "Stop calling tools and reply."}}
                elif ran_money_tool and tc.function.name in MONEY_ADJACENT:
                    result = {"ok": False, "error": {"code": "one_at_a_time", "message": "Call one money-related tool at a time."}}
                else:
                    calls += 1
                    result = self.run_tool(tc.function.name, tc.function.arguments, actor)
                    ran_money_tool |= tc.function.name in MONEY_ADJACENT
                    if result.get("error", {}).get("code") == "schema_invalid":
                        repairs += 1
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
        return self.compose(text)

    def safe(self, text: str) -> str:
        tool_cents = set().union(*(safety.cents_in(d) for _, d in self.tool_results)) if self.tool_results else set()
        typed = set().union(*(safety.amounts_in(t) for t in ChatMessage.objects.filter(group=self.group, direction="in")
                              .order_by("-seq").values_list("text_redacted", flat=True)[:50]))
        caps = {c for c in (m.budget_cap_cents for m in self.group.members.all()) if c is not None}
        out, reason = safety.filter_text(text, tool_cents, typed, caps)
        if reason:
            log_event(EventType.AGENT_TOOL_REJECTED, actor="agent", group_id=self.group.id,
                      payload={"tool": "output", "reason": reason})
            return safety.SAFE_TEXT if self.tool_results else safety.SAFE_TEXT_NO_TOOLS
        return out

    def compose(self, text: str | None) -> list[AgentMessage]:
        text = self.safe(text) if text else text
        labels = {o["id"]: o["title"] for name, d in self.tool_results if name == "search_options" for o in d.get("options", [])}
        attached = [m for name, d in self.tool_results for m in cards.cards_for(name, d, labels, self.group.timezone)]
        out = [AgentMessage(text=text)] if text else []
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
    if inbound:
        refusal = safety.guard(inbound.text or latest_text(group_id, inbound.member_id))
        if refusal:
            log_event(EventType.AGENT_TOOL_REJECTED, actor=actor, group_id=group_id, payload={"tool": "guard", "reason": "injection"})
            return [AgentMessage(text=refusal)]
    return Orchestrator(group_id, **kw).run(actor, event)
