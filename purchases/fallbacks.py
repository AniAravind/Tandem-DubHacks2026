"""
Fallbacks (milestone M6): what to do when someone drops out of a group purchase.

`propose` works out up to three choices, deterministically (no AI guessing at money), and opens a vote on them:

  split_gap     keep the same place, split the full price between the people who are left
  cheaper       switch to a cheaper listing that sleeps everyone left and fits every remaining private cap
  replacement   change nothing, but give the group 12 more hours to invite someone to take the empty spot

Every option shows per_person_cents, delta_cents (change from today's share) and a cap_impact sentence.
The cap_impact sentence only ever says HOW MANY people would be over their limit, never who, because
budgets are private.

Applying the winner is rules.apply_fallback (it moves money, so it lives with the other money rules).
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from core.events import EventType, log_event
from core.models import Member
from purchases import consensus
from purchases.models import Purchase, Share, Vote

S = Share.Status
LIVE_STATUSES = (S.INVITED, S.VIEWING, S.HELD, S.CARD_FAILED)      # a share in one of these is still in the purchase
VOTE_WINDOW = timedelta(hours=2)
REPLACEMENT_EXTENSION = timedelta(hours=12)
REASONS = ("dropout", "decline", "price_change", "deadline_risk", "sold_out")


class FallbackError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def remaining_shares(purchase: Purchase) -> list[Share]:
    """The current (not replaced) shares of people who are still in, oldest member first."""
    return list(Share.objects.filter(purchase=purchase, superseded_by__isnull=True, status__in=LIVE_STATUSES,
                                     member__status=Member.Status.ACTIVE)
                .select_related("member").order_by("member__created_at", "member__display_name"))


def listing_total(listing, people: int) -> int:
    """What the whole group pays for this listing (per-person items multiply by the group size)."""
    return listing.unit_price_cents * people if listing.unit_price_cents else listing.total_cents


def _cap_impact(per_person: int, caps: list[int]) -> str:
    over = sum(1 for c in caps if per_person > c)
    if over == 0:
        return "Within everyone's cap"
    who = "1 person's cap" if over == 1 else f"{over} people's caps"
    return f"Over {who}; they'd need to re-approve"


def build_options(purchase: Purchase, remaining: list[Share]) -> list[dict]:
    from purchases.rules import MAX_SHARE_CENTS                      # imported here to avoid a circular import
    people = len(remaining)
    members = [s.member for s in remaining]
    caps = [c for c in (m.budget_cap_cents for m in members) if c is not None]       # private: used here, never shown
    everyone = Share.objects.filter(purchase=purchase, superseded_by__isnull=True)
    current = min(s.amount_cents for s in everyone)                  # today's base share

    options = []
    gap = consensus.ceil_div(purchase.total_cents, people)
    options.append({"option_id": "split_gap", "label": f"Split the gap: same {purchase.option.title}",
                    "per_person_cents": gap, "delta_cents": gap - current, "cap_impact": _cap_impact(gap, caps)})

    # cheaper: the best-ranked other listing that fits everyone left and everyone's cap
    ranked = consensus.rank_options(members, purchase.option.category, people,
                                    budget_per_person_max_cents=min(caps) if caps else None)
    for c in ranked:
        same = (c.listing.merchant.slug, c.listing.ref) == (purchase.option.merchant_slug, purchase.option.listing_ref)
        if not same and c.total_cents < purchase.total_cents:
            options.append({"option_id": "cheaper", "label": f"Cheaper option: {c.listing.title} (sleeps {c.listing.capacity})",
                            "title": c.listing.title, "merchant_slug": c.listing.merchant.slug, "listing_ref": c.listing.ref,
                            "per_person_cents": c.per_person_cents, "delta_cents": c.per_person_cents - current,
                            "cap_impact": _cap_impact(c.per_person_cents, caps)})
            break

    options.append({"option_id": "replacement", "label": "Invite someone: 12 more hours to fill the spot",
                    "per_person_cents": current, "delta_cents": 0,
                    "cap_impact": "Nobody's share changes; 12 more hours to add someone"})
    return [o for o in options if o["per_person_cents"] <= MAX_SHARE_CENTS]


def _response(purchase: Purchase, vote: Vote, staying: int) -> dict:
    options = [{"key": c["option_id"], **{k: v for k, v in c.items() if k not in ("key", "option_id", "merchant_slug", "listing_ref")}}
               for c in vote.choices]
    return {"purchase_id": str(purchase.id), "vote_id": str(vote.id), "staying": staying, "options": options}


def propose(purchase: Purchase, reason: str, actor: str) -> dict:
    """Work out the options and open the vote. Asking twice while a vote is open returns the same vote."""
    if purchase.state != Purchase.State.COLLECTING:
        raise FallbackError("illegal_state", f"Purchase is {purchase.state}; fallbacks only apply while it is collecting.")
    remaining = remaining_shares(purchase)
    if not remaining:
        raise FallbackError("nobody_left", "Nobody is left in this purchase.")

    open_vote = Vote.objects.filter(purchase=purchase, purpose=Vote.Purpose.FALLBACK, status=Vote.Status.OPEN,
                                    closes_at__gt=timezone.now()).first()
    if open_vote:
        return _response(purchase, open_vote, len(remaining))

    options = build_options(purchase, remaining)
    choices = [{"key": str(i), **o} for i, o in enumerate(options, start=1)]
    vote = Vote.objects.create(
        group_id=purchase.group_id, purchase=purchase, purpose=Vote.Purpose.FALLBACK, choices=choices,
        quorum=consensus.quorum_for(len(remaining)), closes_at=min(timezone.now() + VOTE_WINDOW, purchase.deadline_at))
    log_event(EventType.FALLBACK_PROPOSED, actor=actor, group_id=purchase.group_id, purchase_id=purchase.id,
              payload={"vote_id": str(vote.id), "reason": reason, "options": [o["option_id"] for o in options],
                       "staying": len(remaining)})
    from purchases import tasks                                      # imported here to avoid a circular import
    tasks.schedule(tasks.close_vote, str(vote.id), eta=vote.closes_at)
    return _response(purchase, vote, len(remaining))
