"""Example JSON for every API stub (PHASE_0.md section 6.3). Stub views return these until the real logic lands.

All values are DEMO DATA. Nothing here is a real listing, price, or payment.
"""

FIXTURES = {
    "group_detail": {
        "id": "8c1a...", "name": "Tahoe weekend", "channel": "web", "timezone": "America/Los_Angeles",
        "organizer_id": "a1...",
        "members": [{"id": "a1...", "display_name": "Maya", "role": "organizer", "status": "active", "verified": True}],
    },
    "my_preferences": {
        # Private to the member. Budget never leaves this endpoint and is never sent to the LLM.
        "budget_cap_cents": 19000, "must_haves": ["hot_tub"], "dealbreakers": ["shared_beds"], "flexibility": "firm",
    },
    "options": {"options": [
        {"id": "op1...", "rank": 1, "title": "Pinecrest A-Frame", "merchant_slug": "pinestay", "listing_ref": "pinecrest-a-frame",
         "total_cents": 105000, "per_person_cents": 17500, "group_size": 6, "capacity": 6,
         "features": ["hot_tub", "kitchen", "separate_beds", "parking"], "score": 92,
         "explanation_facts": [
             {"kind": "fits_all_budgets", "per_person_cents": 17500, "lowest_cap_cents": 19000},
             {"kind": "must_have_count", "feature": "hot_tub", "count": 4},
             {"kind": "drive_min", "to": "Heavenly", "minutes": 12},
             {"kind": "tradeoff", "text": "1 bunk room"}],
         "meta": {"demo_data": True, "cancellation": "Free until Nov 7", "photo_url": "/static/demo/pinecrest.jpg"}},
    ]},
    "ballot": {"vote_id": "v1...", "status": "open", "tally": {"1": 4, "2": 1, "3": 0}, "voted": 5, "quorum": 4, "your_choice": "1"},
    "purchase_detail": {
        "id": "pur_7c...", "state": "collecting", "option": {"title": "Pinecrest A-Frame", "merchant_slug": "pinestay"},
        "total_cents": 105000, "held_cents": 70000, "deadline_at": "2026-10-18T02:00:00Z", "version": 1, "simulated": True,
        "shares": [
            {"id": "sh1...", "member": {"id": "a1...", "display_name": "Maya"}, "amount_cents": 17500, "status": "held"},
            {"id": "sh5...", "member": {"id": "a5...", "display_name": "Jordan"}, "amount_cents": 17500, "status": "viewing"},
            {"id": "sh6...", "member": {"id": "a6...", "display_name": "Alex"}, "amount_cents": 17500, "status": "invited"}],
        "counts": {"held": 4, "viewing": 1, "invited": 1, "out": 0, "active": 6},
    },
    "consent_detail": {
        "member": {"id": "a5...", "display_name": "Jordan"}, "invited_by": "Maya", "group_name": "Tahoe weekend",
        "purchase": {"id": "pur_7c...", "version": 1, "title": "Pinecrest A-Frame", "merchant_name": "PineStay Rentals",
                     "merchant_slug": "pinestay", "dates": "Nov 14 to 16", "total_cents": 105000, "split_count": 6,
                     "photo_url": "/static/demo/pinecrest.jpg"},
        "share_cents": 17500, "default_cap_cents": 20000, "deadline_at": "2026-10-18T02:00:00Z",
        "can": ["Hold $175.00 now (not a charge)", "Charge it only if all 6 are in", "Book only Pinecrest A-Frame via PineStay"],
        "can_never": ["Charge more than $200.00", "Pay any other merchant",
                      "Charge you if the group is not fully in by Sat 7:00 PM", "See or store your card number"],
        "consent_text_version": "v1", "consent_text_sha256": "3f9a...", "simulated": True, "offers": [],
    },
    "tokenize": {"agent_token_id": "atk_sim_J8d2", "card": {"brand": "visa", "last4": "1111"}, "simulated": True},
    "approve": {
        "share_status": "held",
        "hold": {"hold_id": "auth_sim_8f2a", "amount_cents": 17500, "status": "AUTHORIZED"},
        "mandate_id": "mdt_sim_Q19", "group_counts": {"held": 5, "active": 6}, "simulated": True,
    },
    "decline": {"share_status": "declined"},
    "reconfirm": {"share_status": "held", "simulated": True},
    "leave": {"share_status": "released"},
    "expense_created": {"expense_id": "ex1...", "splits": [{"member_id": "a1...", "amount_cents": 1200}], "balances_net_cents": 0},
    "ledger": {
        "expenses": [{"id": "ex1...", "description": "Groceries", "payer": "Priya", "amount_cents": 6000,
                      "split_mode": "equal", "split_count": 5}],
        "balances": [{"member_id": "a2...", "display_name": "Priya", "net_cents": 4800},
                     {"member_id": "a1...", "display_name": "Maya", "net_cents": -1200}],
        "net_total_cents": 0,
    },
    "events": {"events": [
        {"seq": 101, "at": "2026-10-17T23:03:40.118Z", "actor": "gateway", "type": "gateway.hold_authorized", "simulated": True,
         "summary": "Hold authorized: Maya $175.00",
         "payload": {"hold_id": "auth_sim_8f2a", "amount_cents": 17500, "member_id": "a1..."}, "hash": "c41e..."}],
        "chain_ok": True},
    "chat_queued": {"message_id": "0f3e...", "seq": 41, "status": "queued"},
    "messages": {"messages": []},
}
