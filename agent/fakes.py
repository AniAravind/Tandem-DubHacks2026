SEED_OPTIONS = [
    {"id": "op-pinecrest", "title": "Pinecrest A-Frame", "per_person_cents": 17500, "total_cents": 105000, "capacity": 6, "score": 92,
     "explanation_facts": [{"kind": "fits_all_budgets", "per_person_cents": 17500, "lowest_cap_cents": 19000},
                           {"kind": "must_have_count", "feature": "hot_tub", "count": 4}, {"kind": "tradeoff", "text": "1 bunk room"}]},
    {"id": "op-lakeview", "title": "Lakeview Bungalow", "per_person_cents": 17800, "total_cents": 106800, "capacity": 7, "score": 81, "explanation_facts": []},
    {"id": "op-cedar", "title": "Cedar Hollow", "per_person_cents": 16200, "total_cents": 97200, "capacity": 6, "score": 74, "explanation_facts": []},
]


class FakeRulesEngine:
    """Canned stand-in for purchases.rules.RulesEngine until the real one is merged (Aryan codes against this)."""

    def execute(self, tool: str, args: dict, actor: str) -> dict:
        if tool == "search_options":
            return {"ok": True, "data": {"options": SEED_OPTIONS}}
        if tool == "start_vote":
            return {"ok": True, "data": {"vote_id": "vote-1", "closes_at": "2026-10-17T22:00:00Z", "quorum": 4}}
        if tool == "create_purchase":
            return {"ok": True, "data": {"purchase_id": "pur-1", "deadline_at": "2026-10-18T02:00:00Z", "consent_links_sent": 6}}
        if tool == "check_status":
            return {"ok": True, "data": {"state": "collecting", "held": 5, "active": 6}}
        if tool == "propose_fallback":
            return {"ok": True, "data": {"options": [
                {"key": "split_gap", "per_person_cents": 21000, "delta_cents": 3500, "cap_impact": "Over 1 person's cap; they'd need to re-approve"},
                {"key": "cheaper", "title": "Juniper Loft", "per_person_cents": 18500, "delta_cents": 1000, "cap_impact": "Within everyone's cap"},
                {"key": "replacement", "per_person_cents": 17500, "delta_cents": 0, "cap_impact": "12 more hours to add a 6th"}]}}
        if tool == "add_expense":
            return {"ok": True, "data": {"expense_id": "ex-1", "per_person_cents": 1200, "split_count": 5}}
        return {"ok": False, "error": {"code": "unknown_tool", "message": tool}}
