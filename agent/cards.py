"""Build chat cards from tool results only (never from numbers the LLM wrote). Shapes match frontend/src/types/contracts.ts."""
from datetime import datetime
from zoneinfo import ZoneInfo

from chat.adapters.base import AgentMessage, Card

FEATURE_LABELS = {"hot_tub": "Hot tub", "kitchen": "Kitchen", "separate_beds": "Separate beds", "parking": "Parking"}


def usd(cents: int) -> str:
    """Chat style: $1,050 for whole dollars, $68.63 otherwise."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}" if cents % 100 == 0 else f"{sign}${cents / 100:,.2f}"


def usd_exact(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def when(iso: str, tz: str = "America/Los_Angeles") -> str:
    """'Sat 7:00 PM' in the group's timezone."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
    return dt.strftime("%a %-I:%M %p")


def _why(facts: list[dict]) -> tuple[list[str], str | None]:
    why, tradeoff = [], None
    for f in facts:
        kind = f.get("kind")
        if kind == "fits_all_budgets":
            why.append(f"Fits everyone's budget (lowest cap {usd(f['lowest_cap_cents'])})")
        elif kind == "must_have_count":
            label = FEATURE_LABELS.get(f["feature"], f["feature"].replace("_", " ").capitalize())
            why.append(f"{label} ({f['count']} of you asked)")
        elif kind == "drive_min":
            why.append(f"{f['minutes']} min to {f['to']}")
        elif kind == "tradeoff":
            tradeoff = f["text"]
    return why, tradeoff


def option_card(o: dict) -> Card:
    meta = o.get("meta") or {}
    why, tradeoff = _why(o.get("explanation_facts") or [])
    return Card("OptionCard", {
        "option_id": o["id"], "rank": o.get("rank", 0), "title": o["title"],
        "merchant_name": o.get("merchant_name") or o.get("merchant_slug", ""),
        "per_person_cents": o["per_person_cents"], "total_cents": o["total_cents"], "capacity": o["capacity"],
        "features": o.get("features", []), "why": why, **({"tradeoff": tradeoff} if tradeoff else {}),
        "fit_score": round(o.get("score") or 0), "cancellation": meta.get("cancellation", ""),
        **({"photo_url": meta["photo_url"]} if meta.get("photo_url") else {}), "demo_data": True,
    })


def vote_card(d: dict, labels: dict | None = None) -> Card:
    labels = labels or {}
    return Card("VoteCard", {
        "vote_id": d["vote_id"], "closes_at": d["closes_at"], "quorum": d["quorum"], "voted": d.get("voted", 0),
        "choices": [{"key": c["key"], "label": c.get("label") or labels.get(c.get("option_id"), c["key"]),
                     "per_person_cents": c.get("per_person_cents", 0), "count": c.get("count", 0)} for c in d["choices"]],
        **({"your_choice": d["your_choice"]} if d.get("your_choice") else {}),
        **({"winner_key": d["winner_key"]} if d.get("winner_key") else {}),
    })


def funding_card(d: dict) -> Card:
    return Card("FundingCard", {
        "purchase_id": d["purchase_id"], "title": d["title"], "total_cents": d["total_cents"],
        "held_cents": d.get("held_cents", 0), "deadline_at": d["deadline_at"],
        "members": [{"id": s["member_id"], "initials": s["display_name"][:1].upper(), "display_name": s["display_name"],
                     "status": s["status"]} for s in d.get("shares", [])],
        "simulated": True,
    })


def fallback_card(d: dict) -> Card:
    return Card("FallbackCard", {
        "purchase_id": d["purchase_id"], "vote_id": d["vote_id"],
        "options": [{"key": o["key"], "label": o.get("label") or o.get("title") or o["key"], "per_person_cents": o["per_person_cents"],
                     "delta_cents": o["delta_cents"], "cap_impact": o["cap_impact"]} for o in d["options"]],
    })


def expense_card(d: dict) -> Card:
    return Card("ExpenseCard", {
        "expense_id": d["expense_id"], "description": d.get("description", ""), "payer": d.get("payer", ""),
        "amount_cents": d["amount_cents"], "split_count": d["split_count"], "per_person_cents": d["per_person_cents"],
    })


def consent_links(d: dict, tz: str) -> list[AgentMessage]:
    """One private message per member with their own consent link."""
    out = []
    for s in d.get("shares", []):
        if not s.get("consent_url"):
            continue
        out.append(AgentMessage(
            text=f"Your share for {d['title']}: {usd(s['amount_cents'])}. Hold it by {when(d['deadline_at'], tz)}. "
                 "It's a hold, not a charge, and you can say no on the page.",
            link=s["consent_url"], private_to_member_id=s["member_id"]))
    return out


UNIT_SUFFIX = {"night": "/night", "person": " per person", "each": " each"}


def live_offers_card(d: dict) -> Card:
    """Shape documented in docs/live-offer-contract.md (LiveOffersCard)."""
    return Card("LiveOffersCard", {"search_id": d["search_id"], "query": d["query"], "offers": d["offers"],
                                   "disclaimer": d["disclaimer"], "live": True, "checkout_ready": False})


def _listed(o: dict) -> str:
    if o.get("price_cents") is None:
        return "price not listed"
    return usd(o["price_cents"]) + UNIT_SUFFIX.get(o.get("price_unit") or "", "") + " listed"


def live_offers_text(d: dict) -> str:
    if not d.get("offers"):
        return ("🌐 I searched the web but didn’t find listings worth sharing for that. "
                "Try a more specific place, item, or date range.")
    lines = ["🌐 Live picks from the web", ""]
    for o in d["offers"]:
        estimate = f" · about {usd(o['est_per_person_cents'])} each" if o.get("est_per_person_cents") is not None else ""
        fit = " · fits your budget" if o.get("fits_budget") else ""
        lines += [f"{o['label']}. {o['title']}", f"{_listed(o)} on {o['source']}{estimate}{fit}", o["url"], ""]
    if any(o.get("kind") == "flight" for o in d["offers"]):
        lines.append("Flight fares are advertised prices only: dates, passenger coverage, baggage and round-trip pricing need checking on the source.\n")
    lines.append("Real listings, not Tandem quotes: prices can change and checkout isn’t available for these. "
                 "Reply with a letter to put one on your planning page, or say “compare A and B”.")
    return "\n".join(lines)


def compare_card(d: dict) -> Card:
    return Card("CompareCard", {"rows": d["rows"], "cheapest_ref": d.get("cheapest_ref"),
                                "budget_per_person_cents": d.get("budget_per_person_cents"), "note": d.get("note", "")})


def compare_text(d: dict) -> str:
    lines = ["⚖️ Side by side", ""]
    for r in d["rows"]:
        bits = [f"{r['ref']}. {r['title']}"]
        if r["kind"] == "catalog":
            bits.append(f"{usd(r['per_person_cents'])} each · {usd(r['total_cents'])} total")
            if r.get("features"):
                bits.append(", ".join(FEATURE_LABELS.get(f, f.replace('_', ' ')) for f in r["features"][:3]))
            if r.get("cancellation"):
                bits.append(r["cancellation"])
            bits.append("demo offer, can be paid in Tandem")
        else:
            bits.append(f"{r['price_text']} listed" if r.get("price_text") else "price not listed")
            if r.get("per_person_cents") is not None:
                bits.append(f"about {usd(r['per_person_cents'])} each")
            bits.append(f"{r['source']}, web only")
        lines.append(" · ".join(bits))
    lines.append("")
    if d.get("cheapest_ref"):
        lines.append(f"Lowest per person: {d['cheapest_ref']}.")
    fits = [r["ref"] for r in d["rows"] if r.get("fits_budget")]
    if d.get("budget_per_person_cents") is not None:
        lines.append(f"Within your {usd(d['budget_per_person_cents'])} budget: " + (", ".join(fits) if fits else "none of these") + ".")
    lines.append("Reply with a number or letter to choose.")
    return "\n".join(lines)


def cards_for(tool: str, data: dict, labels: dict, tz: str) -> list[AgentMessage]:
    """Card messages (text empty) plus private links for one successful tool result."""
    if tool == "search_options":
        return [AgentMessage(text="", card=option_card(o)) for o in data.get("options", [])[:3]]
    if tool == "start_vote":
        return [AgentMessage(text="", card=vote_card(data, labels))]
    if tool == "create_purchase":
        return [AgentMessage(text="", card=funding_card(data))] + consent_links(data, tz)
    if tool == "propose_fallback":
        return [AgentMessage(text="", card=fallback_card(data))]
    if tool == "add_expense":
        return [AgentMessage(text="", card=expense_card(data))]
    if tool == "live_search" and data.get("offers"):
        return [AgentMessage(text="", card=live_offers_card(data))]
    if tool == "compare_options":
        return [AgentMessage(text="", card=compare_card(data))]
    if tool == "check_status" and data.get("shares"):
        return [AgentMessage(text="", card=funding_card(data))]
    return []
