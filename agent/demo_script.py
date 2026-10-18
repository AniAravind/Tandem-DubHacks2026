"""DEMO_SCRIPTED=1: map the canonical demo messages (TANDEM_SPEC 8.3.1) to the exact tool calls, with canned text.

The tool calls still go through the same validation, id checks, engine, events, and output filter as the live path;
only Grok is skipped. Text is filled in from tool results, never hardcoded prices.
Also used as the backup when Grok is unreachable in DEMO_MODE.
"""
import json
import re
from collections import Counter

from agent.cards import usd, usd_exact, when

FIND = re.compile(r"\b(find|search|look for|suggest|options?)\b|\b(cabin|rental|place to stay|airbnb)\b", re.I)
EXPENSE = re.compile(r"\bpaid\s+\$?(\d+(?:\.\d{1,2})?)\s+for\s+(?:the\s+)?([a-z][a-z ]{0,30}?)(?:\s*(?:,|\bsplit\b|\band\b|$))", re.I)
STATUS = re.compile(r"\b(status|how many|where are we|who'?s in)\b", re.I)
EMOJI = {"groceries": "🛒", "gas": "⛽", "dinner": "🍽️", "food": "🍽️", "drinks": "🥤"}


def _members(orch):
    return list(orch.group.members.filter(status="active").order_by("-role", "created_at", "display_name"))


def _turn(orch, actor, calls):
    """Run tool calls in order; stop at the first failure. Returns {tool: data} or None."""
    out = {}
    for name, args in calls:
        result = orch.run_tool(name, json.dumps(args), actor)
        if not result.get("ok"):
            return None
        out[name] = result["data"]
    return out


def find_options(orch, actor, text):
    members = _members(orch)
    wants = Counter(f for m in members for f in (m.prefs or {}).get("must_haves", []))
    search = {"category": "rental", "group_size": len(members), "location": "Lake Tahoe",
              "dates": {"start": "2026-11-14", "end": "2026-11-16"}, "must_haves": [f for f, _ in wants.most_common(3)]}
    got = _turn(orch, actor, [("search_options", search)])
    if not got:
        return None
    options = got["search_options"]["options"][:3]
    if not _turn(orch, actor, [("start_vote", {"option_ids": [o["id"] for o in options], "closes_in_minutes": 60})]):
        return None
    lowest = next((f["lowest_cap_cents"] for o in options for f in o.get("explanation_facts", []) if f["kind"] == "fits_all_budgets"), None)
    top = next((f for f in options[0].get("explanation_facts", []) if f["kind"] == "must_have_count"), None)
    lines = [f"All {len(members)} replied." + (f" Every budget covers at least {usd(lowest)}/person." if lowest else "")]
    if top:
        lines.append(f"{top['count']} of you want a {top['feature'].replace('_', ' ')}.")
    lines.append(f"Here are {len(options)} that fit everyone 👇 Vote by tapping a card, or reply 1, 2, or 3.")
    return "\n".join(lines)


def open_funding(orch, actor, event):
    ids = [str(m.id) for m in _members(orch)]
    got = _turn(orch, actor, [("create_purchase", {"option_id": event["winner_option_id"], "member_ids": ids, "deadline_hours": 6})])
    if not got:
        return None
    d = got["create_purchase"]
    return (f"Next: everyone holds their share. Nothing gets charged unless all {len(ids)} are in by "
            f"{when(d['deadline_at'], orch.group.timezone)}.\nYour link is private, just for you. (Payments are simulated in this demo.)")


def fallback(orch, actor, event):
    got = _turn(orch, actor, [("propose_fallback", {"purchase_id": event["purchase_id"], "reason": "decline"})])
    if not got:
        return None
    d = got["propose_fallback"]
    total = d["staying"] + 1
    return (f"1 spot opened up, so we're at {d['staying']} of {total}. Nobody's been charged.\n"
            "3 ways to keep the trip on. Reply 1, 2, or 3.")


def expense(orch, actor, match):
    member_id = actor.split(":", 1)[1]
    amount = float(match.group(1))
    item = match.group(2).strip().capitalize() or "Shared expense"
    from agent.context import trip_member_ids
    trip = trip_member_ids(orch.group)
    split = {"mode": "equal", **({"member_ids": trip} if member_id in trip else {})}   # dropouts don't share trip costs
    got = _turn(orch, actor, [("add_expense", {"amount": amount, "payer_id": member_id, "description": item, "split": split})])
    if not got:
        return None
    d = got["add_expense"]
    emoji = EMOJI.get(item.lower(), "🧾")
    owes = d.get("owes", [])
    names = ", ".join(o["display_name"] for o in owes)
    tail = f"\nBalances: {names} each owe {d.get('payer', next((m.display_name for m in _members(orch) if str(m.id) == member_id), 'Member'))} {usd_exact(d['per_person_cents'])}." if owes else ""
    return (f"Added {emoji} {d['description']} {usd_exact(d['amount_cents'])}, paid by {d.get('payer', next((m.display_name for m in _members(orch) if str(m.id) == member_id), 'Member'))}, "
            f"split {d['split_count']} ways ({usd_exact(d['per_person_cents'])} each).{tail}")


def status(orch, actor):
    got = _turn(orch, actor, [("check_status", {})])
    if not got:
        return None
    d = got["check_status"]
    waiting = d["active"] - d["held"]
    return f"{d['held']} of {d['active']} held." + (f" Waiting on {waiting} more." if waiting else " Everyone's in.")


def scripted_text(orch, actor: str, text: str, event: dict | None) -> str | None:
    """Text for this turn (tool calls already made through orch), or None if nothing in the script matches."""
    if event and event.get("type") == "vote_closed":
        return open_funding(orch, actor, event)
    if event and event.get("type") == "share_declined":
        return fallback(orch, actor, event)
    if not text or actor == "agent":
        return None
    if m := EXPENSE.search(text):
        return expense(orch, actor, m)
    if STATUS.search(text):
        return status(orch, actor)
    if FIND.search(text):
        return find_options(orch, actor, text)
    return None
