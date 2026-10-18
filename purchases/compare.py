"""Deterministic side-by-side comparison and remembered preferences. The LLM picks what to compare, never the numbers."""
from __future__ import annotations

import uuid

from agent.safety import amounts_in
from chat.services import redact
from purchases import live
from purchases.models import Option
from purchases.serializers import latest_options_json, option_json

MAX_REMEMBERED = 12


class CompareError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _brief(member) -> dict:
    from chat.models import PhotonSession
    session = PhotonSession.objects.filter(member=member).first()
    return session.planning_state if session else {}


def _catalog_row(o: dict) -> dict:
    meta = o.get("meta") or {}
    return {"kind": "catalog", "ref": str(o.get("rank") or ""), "id": o["id"], "title": o["title"],
            "source": o.get("merchant_slug", ""), "per_person_cents": o.get("per_person_cents"),
            "total_cents": o.get("total_cents"), "price_text": None, "features": o.get("features", [])[:5],
            "cancellation": meta.get("cancellation", ""), "url": None, "checkout_ready": True, "demo_data": True}


def _live_row(o) -> dict:
    return {"kind": "live", "ref": o.label, "id": str(o.id), "title": o.title, "source": o.source,
            "per_person_cents": o.est_per_person_cents, "total_cents": None, "price_cents": o.price_cents,
            "price_text": o.price_text or None,
            "features": [], "cancellation": "", "url": o.url, "checkout_ready": False, "demo_data": False}


def compare(member, args: dict) -> dict:
    brief = _brief(member)
    revision = brief.get("revision") if brief else None
    rows = []
    ids = args.get("option_ids") or []
    if ids:
        for oid in ids:
            try:
                option = Option.objects.get(pk=uuid.UUID(str(oid)), group_id=member.group_id)
            except (ValueError, Option.DoesNotExist):
                raise CompareError("not_found", "No such option.")
            if revision and option.meta.get("brief_revision") != revision:
                raise CompareError("stale_option", "That option belongs to an earlier request. Search again for the current goal.")
            rows.append(_catalog_row(option_json(option)))
    labels = args.get("offer_labels") or []
    from chat.models import PhotonSession
    from agent.private_actions import current_live_offers
    session=PhotonSession.objects.filter(member=member).first()
    offers = current_live_offers(member.group,session) if session else live.latest(member.group, revision)
    if labels:
        by_label = {o.label: o for o in offers}
        missing = [label for label in labels if label not in by_label]
        if missing:
            raise CompareError("not_found", "Those web results are not in the current search: " + ", ".join(missing) + ".")
        rows += [_live_row(by_label[label]) for label in labels]
    if not ids and not labels:
        current = [o for o in latest_options_json(member.group)["options"]
                   if not revision or o["meta"].get("brief_revision") == revision]
        rows = [_catalog_row(o) for o in current[:3]] + [_live_row(o) for o in offers[:3]]
    if len(rows) < 2:
        raise CompareError("nothing_to_compare", "Need at least two current options or web results to compare.")
    budget = brief.get("budget_per_person_cents") if brief else None     # private planning only: the user's own budget
    priced = [r for r in rows if r["per_person_cents"] is not None]
    cheapest = min(priced, key=lambda r: r["per_person_cents"]) if priced else None
    for r in rows:
        r["fits_budget"] = (r["per_person_cents"] <= budget) if budget is not None and r["per_person_cents"] is not None else None
    return {"rows": rows, "cheapest_ref": cheapest["ref"] if cheapest else None,
            "budget_per_person_cents": budget, "group_size": brief.get("group_size") if brief else None,
            "note": live.DISCLAIMER if any(r["kind"] == "live" for r in rows) else "Demo catalog offers; payments are simulated."}


def remember(member, args: dict) -> dict:
    from purchases.plans import private_planner
    if not private_planner(member):
        raise CompareError("private_planning_only", "Preferences are remembered in your individual conversation.")
    items = [redact(v.strip()) for v in args.get("remember", []) if v.strip()]
    if any(amounts_in(v) or "http" in v.lower() or "[" in v for v in items):
        raise CompareError("not_a_preference", "Remember tastes and constraints, not budgets, links, or contact details. Budgets are confirmed per purchase.")
    prefs = dict(member.prefs or {})
    saved = [v for v in prefs.get("remembered", [])]
    forget = {v.strip().casefold() for v in args.get("forget", [])}
    saved = [v for v in saved if v.casefold() not in forget and not any(f in v.casefold() for f in forget)]
    for v in items:
        if v.casefold() not in {s.casefold() for s in saved}:
            saved.append(v)
    prefs["remembered"] = saved[-MAX_REMEMBERED:]
    member.prefs = prefs
    member.save(update_fields=["prefs"])
    return {"remembered": prefs["remembered"]}
