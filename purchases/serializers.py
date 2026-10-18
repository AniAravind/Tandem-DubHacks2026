"""
Turns database objects into the exact JSON shapes promised in the API contract (PHASE_0.md section 6.3).

Plain functions that return plain dicts, no framework magic. One rule above all:
budgets (`budget_cap_cents`) are private. They are only ever returned to their owner by the preferences endpoint,
and they never appear in any other response or in any event.
"""
from __future__ import annotations

from datetime import timezone as dt_timezone

from core.events import verify_chain
from core.models import EventLog, Group, Member
from payments.models import Hold
from purchases import ledger
from purchases.models import Expense, Option, Purchase, Share, Vote

S = Share.Status

# Payload keys that must never be shown on the dashboard, even if some code logged them by mistake.
FORBIDDEN_KEY_PARTS = ("budget", "phone")                      # dropped if the key CONTAINS these
FORBIDDEN_KEYS = ("pan", "card_number", "cvv", "otp", "otp_code",   # dropped only if the key IS one of these
                  "cap", "cap_cents", "default_cap_cents", "max_amount_cents", "mandate_remaining_cents")                  # a member's cap can hint at their private budget


def iso(dt) -> str | None:
    """2026-10-18T02:00:00Z style timestamps (always UTC, always ending in Z)."""
    if dt is None:
        return None
    return dt.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


# ---------------------------------------------------------------- group
def group_json(group: Group) -> dict:
    members = group.members.order_by("created_at", "display_name")
    return {
        "id": str(group.id), "name": group.name, "channel": group.channel, "timezone": group.timezone,
        "organizer_id": str(group.organizer_id) if group.organizer_id else None,
        "members": [{"id": str(m.id), "display_name": m.display_name, "role": m.role, "status": m.status,
                     "verified": m.verified_at is not None} for m in members],       # never budgets
    }


# ---------------------------------------------------------------- preferences (private to the caller)
def preferences_json(member: Member) -> dict:
    prefs = member.prefs or {}
    return {"budget_cap_cents": member.budget_cap_cents, "must_haves": prefs.get("must_haves", []),
            "dealbreakers": prefs.get("dealbreakers", []), "flexibility": prefs.get("flexibility", "firm")}


# ---------------------------------------------------------------- options
INTERNAL_META_KEYS = ("search_id", "rank", "per_person_cents", "group_size")


def option_json(option: Option) -> dict:
    meta = option.meta or {}
    return {
        "id": str(option.id), "rank": meta.get("rank"), "title": option.title, "merchant_slug": option.merchant_slug,
        "listing_ref": option.listing_ref, "total_cents": option.total_cents,
        "per_person_cents": meta.get("per_person_cents"), "group_size": meta.get("group_size"),
        "capacity": option.capacity, "features": option.features, "score": round(option.score or 0),
        "explanation_facts": option.explanation_facts,
        "meta": {k: v for k, v in meta.items() if k not in INTERNAL_META_KEYS},
    }


def latest_options_json(group: Group) -> dict:
    """The options from the group's most recent search, best first."""
    from django.conf import settings
    from chat.models import PhotonSession
    if settings.PRIVATE_LIVE_ONLY and PhotonSession.objects.filter(member__group=group).exists():
        return {'options': []}
    newest = Option.objects.filter(group=group).order_by("-created_at").first()
    if newest is None:
        return {"options": []}
    batch = [o for o in Option.objects.filter(group=group, meta__search_id=newest.meta.get("search_id"))]
    batch.sort(key=lambda o: o.meta.get("rank") or 99)
    return {"options": [option_json(o) for o in batch]}


# ---------------------------------------------------------------- votes
def vote_json(vote: Vote, member: Member) -> dict:
    ballots = list(vote.ballots.all())
    tally = {c["key"]: 0 for c in vote.choices}
    for b in ballots:
        tally[b.choice_key] = tally.get(b.choice_key, 0) + 1
    mine = next((b.choice_key for b in ballots if b.member_id == member.id), None)
    return {"vote_id": str(vote.id), "status": vote.status, "tally": tally, "voted": len(ballots),
            "quorum": vote.quorum, "your_choice": mine}


# ---------------------------------------------------------------- purchase
def active_shares(purchase: Purchase):
    return (purchase.shares.filter(superseded_by__isnull=True).select_related("member")
            .order_by("created_at", "member__display_name"))


def purchase_json(purchase: Purchase) -> dict:
    shares = list(active_shares(purchase))
    # "held" also counts captured shares (money secured); "viewing" also counts card_failed (still pending);
    # "out" is anyone who is no longer in (declined, released, refunded).
    counts = {
        "held": sum(1 for s in shares if s.status in (S.HELD, S.CAPTURED)),
        "viewing": sum(1 for s in shares if s.status in (S.VIEWING, S.CARD_FAILED)),
        "invited": sum(1 for s in shares if s.status == S.INVITED),
        "out": sum(1 for s in shares if s.status in (S.DECLINED, S.RELEASED, S.REFUNDED)),
        "active": sum(1 for s in shares if s.member.status == Member.Status.ACTIVE),
    }
    return {
        "id": str(purchase.id), "state": purchase.state,
        "option": {"title": purchase.option.title, "merchant_slug": purchase.option.merchant_slug},
        "total_cents": purchase.total_cents,
        "held_cents": sum(s.amount_cents for s in shares if s.status in (S.HELD, S.CAPTURED)),
        "deadline_at": iso(purchase.deadline_at), "version": purchase.version, "simulated": True,
        "shares": [{"id": str(s.id), "member": {"id": str(s.member_id), "display_name": s.member.display_name},
                    "amount_cents": s.amount_cents, "status": s.status} for s in shares],
        "counts": counts,
    }


# ---------------------------------------------------------------- ledger
def ledger_json(group: Group) -> dict:
    expenses = []
    for e in Expense.objects.filter(group=group).select_related("payer").prefetch_related("splits").order_by("created_at"):
        expenses.append({"id": str(e.id), "description": e.description, "payer": e.payer.display_name,
                         "amount_cents": e.amount_cents, "split_mode": e.split_mode, "split_count": len(e.splits.all())})
    balances = ledger.compute_balances(group)
    names = {b["member_id"]: b["display_name"] for b in balances}
    settlements = [{"from": names[p["from"]], "to": names[p["to"]], "amount_cents": p["amount_cents"]}
                   for p in ledger.settle_up(balances)]
    return {"expenses": expenses, "balances": balances, "net_total_cents": sum(b["net_cents"] for b in balances),
            "settle_up": settlements}


# ---------------------------------------------------------------- events
def _clean_payload(payload: dict) -> dict:
    """Remove private fields at every depth before exposing an event to the group."""
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()
                    if k.lower() not in FORBIDDEN_KEYS and k.lower() != "simulated"
                    and not any(bad in k.lower() for bad in FORBIDDEN_KEY_PARTS)}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    return clean(payload or {})


def _summary(e: EventLog, names: dict) -> str:
    p, t = e.payload or {}, e.type
    who = names.get(p.get("member_id") or p.get("share_id"), "")
    amount = money(p["amount_cents"]) if isinstance(p.get("amount_cents"), int) else ""
    if t == "gateway.hold_authorized":
        return f"Hold authorized: {who or 'member'} {amount or money((p.get('amount') or {}).get('amount_cents', 0))}".strip()
    if t == "gateway.hold_declined":
        return f"Hold declined ({p.get('decline_code', 'declined')})"
    if t == "gateway.hold_released":
        return f"Hold released ({p.get('reason', 'released')})"
    if t == "gateway.captured":
        return f"Captured {money((p.get('amount') or {}).get('amount_cents', 0))}"
    if t == "gateway.refunded":
        return f"Refunded {money((p.get('amount') or {}).get('amount_cents', 0))}"
    if t == "gateway.mandate_created":
        return "Mandate created"
    if t == "gateway.mandate_revoked":
        return "Mandate revoked"
    if t == "purchase.state_changed":
        return f"Purchase {p.get('from')} → {p.get('to')}"
    if t == "share.state_changed":
        return f"{who or 'A member'}'s share {p.get('from')} → {p.get('to')}"
    if t == "vote.ballot_cast":
        return "A ballot was cast"
    if t == "prefs.updated":
        return "Preferences updated"
    if t == "booking.confirmed":
        return "Booking confirmed"
    if t.startswith("merchant."):
        return f"Merchant {'accepted' if t.endswith('accepted') else 'rejected'} the request" + (
            f" ({p['reason']})" if p.get("reason") else "")
    return t.replace(".", " ").replace("_", " ").capitalize()


def events_json(group: Group, after_seq: int = 0, limit: int = 200) -> dict:
    rows = list(EventLog.objects.filter(group=group, seq__gt=after_seq).order_by("seq")[:limit])
    # one lookup for every name we want to mention in summaries
    share_ids = {r.payload.get("share_id") for r in rows if r.payload.get("share_id")}
    names = {str(s.id): s.member.display_name for s in Share.objects.filter(id__in=share_ids).select_related("member")}
    names.update({str(m.id): m.display_name for m in group.members.all()})
    for r in rows:                                  # gateway hold events: find the member through the Hold row
        hid = r.payload.get("hold_id")
        if r.type == "gateway.hold_authorized" and hid:
            hold = Hold.objects.filter(gateway_hold_id=hid).select_related("share__member").first()
            if hold:
                r.payload = {**r.payload, "member_id": str(hold.share.member_id), "amount_cents": hold.amount_cents}
                names[str(hold.share.member_id)] = hold.share.member.display_name
    events = [{"seq": r.seq, "at": iso(r.created_at), "actor": r.actor, "type": r.type, "simulated": r.simulated,
               "summary": _summary(r, names), "payload": _clean_payload(r.payload), "hash": r.hash} for r in rows]
    # chain_ok re-hashes from the very first event up to the newest one we are returning.
    return {"events": events, "chain_ok": verify_chain(up_to_seq=rows[-1].seq if rows else None)}
