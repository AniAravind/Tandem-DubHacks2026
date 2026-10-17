"""FakeRulesEngine: stand-in for purchases.rules.RulesEngine until the real one is merged (Aryan codes against this).

Same interface as the real engine: execute(tool, args, actor) -> {"ok": true, "data": ...} | {"ok": false, "error": ...}.
It keeps a little per-group state (options, open vote, purchase) so a whole demo conversation works end to end.
State lives in Redis when it is reachable (web and worker dynos share it), else in this process.
All prices are DEMO DATA. Nothing here moves money.
"""
import json
import math
import ssl
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

SEED_OPTIONS = [
    {"id": "op-pinecrest", "title": "Pinecrest A-Frame", "per_person_cents": 17500, "total_cents": 105000, "capacity": 6, "score": 92,
     "explanation_facts": [{"kind": "fits_all_budgets", "per_person_cents": 17500, "lowest_cap_cents": 19000},
                           {"kind": "must_have_count", "feature": "hot_tub", "count": 4}, {"kind": "tradeoff", "text": "1 bunk room"}]},
    {"id": "op-lakeview", "title": "Lakeview Bungalow", "per_person_cents": 17800, "total_cents": 106800, "capacity": 7, "score": 81, "explanation_facts": []},
    {"id": "op-cedar", "title": "Cedar Hollow", "per_person_cents": 16200, "total_cents": 97200, "capacity": 6, "score": 74, "explanation_facts": []},
]
OPTION_DETAILS = {   # card details the real ranking API also returns (features, meta); demo data
    "op-pinecrest": {"merchant_slug": "pinestay", "merchant_name": "PineStay", "features": ["hot_tub", "kitchen", "separate_beds", "parking"],
                     "meta": {"demo_data": True, "cancellation": "Free cancel until Nov 7", "drive_min": 12, "to": "Heavenly", "beds": "3 bd"}},
    "op-lakeview": {"merchant_slug": "pinestay", "merchant_name": "PineStay", "features": ["kitchen", "separate_beds", "parking"],
                    "meta": {"demo_data": True, "cancellation": "Free cancel until Nov 10", "drive_min": 18, "to": "Heavenly"},
                    "explanation_facts": [{"kind": "fits_all_budgets", "per_person_cents": 17800, "lowest_cap_cents": 19000},
                                          {"kind": "tradeoff", "text": "No hot tub"}]},
    "op-cedar": {"merchant_slug": "pinestay", "merchant_name": "PineStay", "features": ["kitchen", "separate_beds"],
                 "meta": {"demo_data": True, "cancellation": "Non-refundable", "drive_min": 25, "to": "Heavenly"},
                 "explanation_facts": [{"kind": "fits_all_budgets", "per_person_cents": 16200, "lowest_cap_cents": 19000},
                                       {"kind": "tradeoff", "text": "No hot tub, non-refundable"}]},
}
JUNIPER = {"id": "op-juniper", "title": "Juniper Loft", "total_cents": 92500, "capacity": 5}

_LOCAL: dict[str, dict] = {}
_redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        try:
            import redis
            url = settings.REDIS_URL
            kw = {"ssl_cert_reqs": ssl.CERT_NONE} if url.startswith("rediss://") else {}
            client = redis.from_url(url, socket_timeout=1, **kw)
            client.ping()
            _redis_client = client
        except Exception:                                    # noqa: BLE001  (no Redis locally is fine)
            _redis_client = False
    return _redis_client or None


def _load(group_id: str) -> dict:
    r = _redis()
    if r:
        raw = r.get(f"tandem:fake:{group_id}")
        return json.loads(raw) if raw else {}
    return json.loads(json.dumps(_LOCAL.get(group_id, {})))


def _save(group_id: str, state: dict) -> None:
    r = _redis()
    if r:
        r.set(f"tandem:fake:{group_id}", json.dumps(state), ex=7 * 24 * 3600)
    else:
        _LOCAL[group_id] = json.loads(json.dumps(state))


def reset(group_id: str) -> None:
    r = _redis()
    if r:
        r.delete(f"tandem:fake:{group_id}")
    _LOCAL.pop(str(group_id), None)


def _ok(data: dict) -> dict:
    return {"ok": True, "data": data}


def _err(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def split_cents(total: int, n: int) -> list[int]:
    """Equal split in whole cents; leftover cents go to the first shares (the organizer is listed first)."""
    base, extra = divmod(total, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


class FakeRulesEngine:
    def __init__(self, group_id=None):
        self.group_id = str(group_id) if group_id else None

    # ------------------------------------------------------------------ helpers
    def _members(self):
        from core.models import Member
        return list(Member.objects.filter(group_id=self.group_id, status="active").order_by("-role", "created_at", "display_name"))

    def _cap_impact(self, per_person_cents: int, member_ids: list[str]) -> str:
        from core.models import Member
        over = sum(1 for m in Member.objects.filter(pk__in=member_ids)
                   if m.budget_cap_cents is not None and m.budget_cap_cents < per_person_cents)
        if over == 0:
            return "Within everyone's cap"
        return f"Over {over} {'person' if over == 1 else 'people'}'s cap{'s' if over > 1 else ''}; they'd need to re-approve"

    def known_ids(self) -> set[str]:
        s = _load(self.group_id)
        ids = {o["id"] for o in s.get("options", [])} | {JUNIPER["id"]}
        for key in ("vote", "purchase"):
            if s.get(key):
                ids.add(s[key]["id"])
        return ids

    def snapshot(self) -> dict:
        """What group_state shows Grok (no budgets)."""
        s = _load(self.group_id)
        out = {"options": [{"id": o["id"], "title": o["title"]} for o in s.get("options", [])]}
        if s.get("vote"):
            v = s["vote"]
            out["open_vote"] = None if v["status"] != "open" else {
                "id": v["id"], "purpose": v["purpose"], "closes_at": v["closes_at"], "quorum": v["quorum"],
                "voted": len(v["ballots"]), "choices": [{"key": c["key"], "label": c["label"]} for c in v["choices"]]}
        if s.get("purchase"):
            p = s["purchase"]
            out["purchase"] = {"id": p["id"], "title": p["title"], "state": p["state"], "deadline_at": p["deadline_at"],
                               "counts": _counts(p)}
        return out

    # ------------------------------------------------------------------ the 6 tools
    def execute(self, tool: str, args: dict, actor: str) -> dict:
        handler = getattr(self, f"_tool_{tool}", None)
        if handler is None:
            return _err("unknown_tool", tool)
        return handler(args, actor)

    def _tool_search_options(self, args, actor):
        options = []
        for rank, o in enumerate(SEED_OPTIONS, start=1):
            d = OPTION_DETAILS[o["id"]]
            options.append({**o, "rank": rank, "merchant_slug": d["merchant_slug"], "merchant_name": d["merchant_name"],
                            "features": d["features"], "meta": d["meta"],
                            "explanation_facts": o["explanation_facts"] or d.get("explanation_facts", [])})
        s = _load(self.group_id)
        s["options"] = options
        _save(self.group_id, s)
        return _ok({"options": options, "demo_data": True})

    def _tool_start_vote(self, args, actor):
        s = _load(self.group_id)
        by_id = {o["id"]: o for o in s.get("options", [])}
        missing = [i for i in args["option_ids"] if i not in by_id]
        if missing:
            return _err("unknown_option", "Search for options first, then vote on the ids it returned.")
        if s.get("vote") and s["vote"]["status"] == "open":
            return _err("illegal_state", "A vote is already open.")
        active = len(self._members())
        closes = timezone.now() + timedelta(minutes=args.get("closes_in_minutes", 60))
        n = len(s.get("votes_made", [])) + 1
        vote = {"id": f"vote-{n}", "purpose": "pick_option", "status": "open", "closes_at": closes.isoformat(),
                "quorum": active // 2 + 1, "ballots": {},
                "choices": [{"key": str(i), "option_id": oid, "label": by_id[oid]["title"],
                             "per_person_cents": by_id[oid]["per_person_cents"]} for i, oid in enumerate(args["option_ids"], 1)]}
        s["vote"] = vote
        s.setdefault("votes_made", []).append(vote["id"])
        _save(self.group_id, s)
        return _ok(_vote_data(vote))

    def _tool_create_purchase(self, args, actor):
        s = _load(self.group_id)
        option = next((o for o in s.get("options", []) if o["id"] == args["option_id"]), None)
        if option is None:
            return _err("unknown_option", "That option is not in this group's search results.")
        if s.get("purchase") and s["purchase"]["state"] in ("collecting", "funded", "booked"):
            return _err("illegal_state", "This group already has a purchase in progress.")
        members = {str(m.id): m for m in self._members()}
        ids = [i for i in args["member_ids"] if i in members]
        amounts = split_cents(option["total_cents"], len(ids))
        deadline = timezone.now() + timedelta(hours=args["deadline_hours"])
        purchase = {"id": "pur-1", "option_id": option["id"], "title": option["title"], "total_cents": option["total_cents"],
                    "state": "collecting", "deadline_at": deadline.isoformat(), "version": 1,
                    "shares": [{"member_id": mid, "display_name": members[mid].display_name, "amount_cents": amt,
                                "status": "invited", "consent_url": f"/c/demo-{mid[:8]}"} for mid, amt in zip(ids, amounts)]}
        s["purchase"] = purchase
        _save(self.group_id, s)
        return _ok({**_purchase_data(purchase), "consent_links_sent": len(ids), "simulated": True})

    def _tool_check_status(self, args, actor):
        p = _load(self.group_id).get("purchase")
        if not p:
            return _err("no_purchase", "There is no purchase yet.")
        return _ok({"purchase_id": p["id"], "state": p["state"], **_counts(p)})

    def _tool_propose_fallback(self, args, actor):
        s = _load(self.group_id)
        p = s.get("purchase")
        if not p or p["id"] != args["purchase_id"]:
            return _err("no_purchase", "That purchase is not open in this group.")
        staying = [sh for sh in p["shares"] if sh["status"] not in ("declined", "released")]
        ids = [sh["member_id"] for sh in staying]
        n = max(len(staying), 1)
        current = p["shares"][0]["amount_cents"]
        gap = math.ceil(p["total_cents"] / n)
        cheaper = math.ceil(JUNIPER["total_cents"] / n)
        options = [
            {"key": "split_gap", "label": f"Split the gap: same {p['title']}", "per_person_cents": gap,
             "delta_cents": gap - current, "cap_impact": self._cap_impact(gap, ids)},
            {"key": "cheaper", "label": f"Cheaper option: {JUNIPER['title']} (sleeps {JUNIPER['capacity']})",
             "per_person_cents": cheaper, "delta_cents": cheaper - current, "cap_impact": self._cap_impact(cheaper, ids)},
            {"key": "replacement", "label": "Invite someone: 12 more hours to add a 6th", "per_person_cents": current,
             "delta_cents": 0, "cap_impact": "Within everyone's cap"},
        ]
        n_votes = len(s.get("votes_made", [])) + 1
        vote = {"id": f"vote-{n_votes}", "purpose": "fallback", "status": "open", "quorum": len(staying) // 2 + 1,
                "closes_at": (timezone.now() + timedelta(hours=2)).isoformat(), "ballots": {}, "voters": ids,
                "choices": [{"key": str(i), "option_id": o["key"], "label": o["label"], "per_person_cents": o["per_person_cents"]}
                            for i, o in enumerate(options, 1)]}
        s["vote"] = vote
        s.setdefault("votes_made", []).append(vote["id"])
        _save(self.group_id, s)
        return _ok({"purchase_id": p["id"], "vote_id": vote["id"], "staying": len(staying), "options": options})

    def _tool_add_expense(self, args, actor):
        members = {str(m.id): m for m in self._members()}
        if args["payer_id"] not in members:
            return _err("unknown_member", "The payer is not in this group.")
        p = _load(self.group_id).get("purchase")
        split_ids = args["split"].get("member_ids") or (
            [sh["member_id"] for sh in p["shares"] if sh["status"] not in ("declined", "released")] if p else list(members))
        split_ids = [i for i in split_ids if i in members] or list(members)
        amount = round(args["amount"] * 100)
        parts = split_cents(amount, len(split_ids))
        payer = members[args["payer_id"]]
        owes = [{"member_id": mid, "display_name": members[mid].display_name, "owes_cents": amt}
                for mid, amt in zip(split_ids, parts) if mid != args["payer_id"]]
        return _ok({"expense_id": "ex-1", "description": args.get("description") or "Shared expense", "payer_id": str(payer.id),
                    "payer": payer.display_name, "amount_cents": amount, "split_count": len(split_ids),
                    "per_person_cents": parts[-1], "owes": owes, "balances_net_cents": 0})

    # ------------------------------------------------------------------ not tools: chat replies and demo hooks
    def open_vote(self) -> dict | None:
        v = _load(self.group_id).get("vote")
        return v if v and v["status"] == "open" else None

    def cast_ballot(self, vote_id: str, member_id: str, choice_key: str) -> dict:
        s = _load(self.group_id)
        v = s.get("vote")
        if not v or v["id"] != vote_id or v["status"] != "open":
            return _err("vote_closed", "That vote is closed.")
        if choice_key not in {c["key"] for c in v["choices"]}:
            return _err("invalid_choice", "That is not one of the choices.")
        if v.get("voters") and member_id not in v["voters"]:
            return _err("not_a_voter", "Only people still in the purchase vote on this.")
        v["ballots"][member_id] = choice_key
        tally = _tally(v)
        top_key, top = max(tally.items(), key=lambda kv: kv[1])
        if top >= v["quorum"]:
            v["status"], v["winner_key"] = "closed", top_key
            if v["purpose"] == "fallback":
                self._apply_fallback(s, v)
        _save(self.group_id, s)
        return _ok({**_vote_data(v, member_id), "status": v["status"], "winner_key": v.get("winner_key")})

    def _apply_fallback(self, s: dict, v: dict) -> None:
        p = s["purchase"]
        choice = next(c for c in v["choices"] if c["key"] == v["winner_key"])
        staying = [sh for sh in p["shares"] if sh["status"] not in ("declined", "released")]
        if choice["option_id"] == "cheaper":
            p.update(option_id=JUNIPER["id"], title=JUNIPER["title"], total_cents=JUNIPER["total_cents"])
        if choice["option_id"] in ("cheaper", "split_gap"):
            p["version"] += 1
            for sh, amt in zip(staying, split_cents(p["total_cents"], len(staying))):
                sh.update(amount_cents=amt, status="viewing")
            p["shares"] = staying
        else:
            p["deadline_at"] = (timezone.now() + timedelta(hours=12)).isoformat()

    def set_share_status(self, member_id: str, status: str) -> dict | None:
        """Demo hook standing in for the consent page: held / declined."""
        s = _load(self.group_id)
        p = s.get("purchase")
        if not p:
            return None
        for sh in p["shares"]:
            if sh["member_id"] == member_id:
                sh["status"] = status
        _save(self.group_id, s)
        return _purchase_data(p)

    def purchase_data(self) -> dict | None:
        p = _load(self.group_id).get("purchase")
        return _purchase_data(p) if p else None


def _tally(v: dict) -> dict:
    tally = {c["key"]: 0 for c in v["choices"]}
    for key in v["ballots"].values():
        tally[key] += 1
    return tally


def _vote_data(v: dict, member_id: str | None = None) -> dict:
    tally = _tally(v)
    return {"vote_id": v["id"], "purpose": v["purpose"], "closes_at": v["closes_at"], "quorum": v["quorum"],
            "voted": len(v["ballots"]), "your_choice": v["ballots"].get(member_id) if member_id else None,
            "winner_key": v.get("winner_key"),
            "choices": [{**c, "count": tally[c["key"]]} for c in v["choices"]]}


def _counts(p: dict) -> dict:
    statuses = [sh["status"] for sh in p["shares"]]
    out = sum(1 for x in statuses if x in ("declined", "released"))
    return {"held": statuses.count("held"), "viewing": statuses.count("viewing"), "invited": statuses.count("invited"),
            "out": out, "active": len(statuses) - out}


def _purchase_data(p: dict) -> dict:
    return {"purchase_id": p["id"], "title": p["title"], "total_cents": p["total_cents"], "state": p["state"],
            "version": p["version"], "deadline_at": p["deadline_at"],
            "held_cents": sum(sh["amount_cents"] for sh in p["shares"] if sh["status"] == "held"),
            "shares": p["shares"], "counts": _counts(p)}
