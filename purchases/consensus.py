"""
The consensus engine (brief section 5). It is plain, deterministic maths: the same inputs always give the
same ranking. The AI only explains the result; it never picks.

Private budgets are used HERE, on the server, to score options. They are never returned. The only budget-related
number that leaves this file is `lowest_cap_cents` in the "fits_all_budgets" fact (the smallest cap in the group,
with no name attached).

For every option o and the n people in the group:
  1. Hard filters: it must hold n people, no dealbreaker feature, and (strict mode) cost per person <= the lowest cap.
  2. Each person's satisfaction sat_i(o) is a number from 0 to 1:
         0.5 * (their must-haves that the option has / their must-haves)
       + 0.3 * budget comfort (0 spare room scores 0.4, 20% or more spare room scores 1.0)
       + 0.2 * vibe match (always 0 for now: nobody has vibe tags yet)
  3. Group score G = mean(sat) - 0.5 * stdev(sat). A happy group beats a lopsided one. score = round(100 * G).
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from django.db.models import Count
from django.utils import timezone

from core.events import EventType, log_event
from core.models import Member
from merchant.models import Listing
from purchases.models import Ballot, Share, Vote

TOP_N = 3


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def active_members(group_id) -> list[Member]:
    return list(Member.objects.filter(group_id=group_id, status=Member.Status.ACTIVE).order_by("created_at", "display_name"))


def eligible_voters(vote: Vote) -> list[Member]:
    """Who may vote. Normally every active member. On a fallback vote, only people still in the purchase
    (somebody who just said "not for me" does not get a say in how the rest of the group pays)."""
    if vote.purpose == Vote.Purpose.FALLBACK and vote.purchase_id:
        still_in = Share.objects.filter(purchase_id=vote.purchase_id, superseded_by__isnull=True,
                                        status__in=(Share.Status.INVITED, Share.Status.VIEWING, Share.Status.HELD,
                                                    Share.Status.CARD_FAILED)).values("member_id")
        return list(Member.objects.filter(id__in=still_in, status=Member.Status.ACTIVE).order_by("created_at", "display_name"))
    return active_members(vote.group_id)


@dataclass
class Candidate:
    listing: Listing
    total_cents: int
    per_person_cents: int
    score: int
    over_budget_count: int
    facts: list = field(default_factory=list)


def _budget_comfort(cap: int | None, share: int) -> float:
    """0 = over budget, 0.4 = right at the cap, 1.0 = 20% or more spare room. Unknown cap = neutral 0.7."""
    if cap is None:
        return 0.7
    headroom = (cap - share) / cap
    if headroom < 0:
        return 0.0
    return 0.4 + 0.6 * min(headroom / 0.2, 1.0)


def _satisfaction(must_haves: set, features: set, cap: int | None, share: int) -> float:
    musts = 0.5 * len(must_haves & features) / max(1, len(must_haves))
    return musts + 0.3 * _budget_comfort(cap, share) + 0.2 * 0.0       # vibe match is 0 until vibe tags exist


CATEGORY_ALIASES = {
    "rental": {"rental", "stay", "lodging", "cabin", "house", "airbnb", "vacation_rental", "hotel", "trip", "travel",
               "accommodation", "cabin_rental", "condo", "villa"},
    "gift_bundle": {"gift", "gift_bundle", "present", "birthday_gift", "group_gift", "gift_box"},
    "furniture": {"furniture", "couch", "sofa", "sectional", "desk", "bed", "table", "chair"},
    "electronics": {"electronics", "console", "game_console", "tv", "television", "laptop", "headphones", "gaming"},
    "passes": {"passes", "pass", "ski_pass", "lift_ticket", "ticket", "festival_pass", "season_pass"},
    "flights": {"flight", "flights", "airfare", "plane_ticket"},
}
_ALIAS = {alias: canonical for canonical, aliases in CATEGORY_ALIASES.items() for alias in aliases}


def canonical_category(category: str) -> str:
    """'Gift', 'birthday gifts', 'Cabin rental' -> the catalog's own category; unknown categories pass through."""
    key = re.sub(r"[^a-z]+", "_", category.lower()).strip("_")
    for candidate in (key, key.rstrip("s"), key.removesuffix("es")):
        if candidate in _ALIAS:
            return _ALIAS[candidate]
    return key or category


def rank_options(members: list[Member], category: str, group_size: int, extra_must_haves=(),
                 budget_per_person_max_cents: int | None = None) -> list[Candidate]:
    """Return the top options, best first. Tries strict (everyone can afford it) and falls back to lenient."""
    caps = {m.id: m.budget_cap_cents for m in members}                  # decrypted here, on the server only
    known = [c for c in caps.values() if c is not None]
    lowest_cap = min(known) if known else None
    wants = {m.id: set((m.prefs or {}).get("must_haves", [])) | set(extra_must_haves) for m in members}
    dealbreakers = set().union(*[set((m.prefs or {}).get("dealbreakers", [])) for m in members]) if members else set()

    pool = []
    for listing in Listing.objects.filter(available=True, merchant__category=category).select_related("merchant"):
        features = set(listing.features)
        if listing.capacity < group_size or features & dealbreakers:
            continue
        if listing.unit_price_cents:                                    # priced per person (passes)
            total, share = listing.unit_price_cents * group_size, listing.unit_price_cents
        else:
            total, share = listing.total_cents, ceil_div(listing.total_cents, group_size)
        if budget_per_person_max_cents is not None and share > budget_per_person_max_cents:
            continue
        pool.append((listing, features, total, share))

    def build(strict: bool) -> list[Candidate]:
        out = []
        for listing, features, total, share in pool:
            if strict and lowest_cap is not None and share > lowest_cap:
                continue
            sats = [_satisfaction(wants[m.id], features, caps[m.id], share) for m in members]
            g = statistics.fmean(sats) - 0.5 * (statistics.pstdev(sats) if len(sats) > 1 else 0.0)
            over = sum(1 for c in caps.values() if c is not None and share > c)
            out.append(Candidate(listing, total, share, max(0, round(100 * g)), over,
                                 _facts(listing, features, share, lowest_cap, over, wants)))
        # best score first; ties go to the cheaper option, then the name, so the order never changes
        out.sort(key=lambda c: (-c.score, c.per_person_cents, c.listing.title))
        return out[:TOP_N]

    return build(strict=True) or build(strict=False)


def _facts(listing: Listing, features: set, share: int, lowest_cap, over: int, wants: dict) -> list:
    """The structured facts the AI is allowed to quote. Nothing else may be claimed about an option."""
    facts = []
    if over == 0 and lowest_cap is not None:
        facts.append({"kind": "fits_all_budgets", "per_person_cents": share, "lowest_cap_cents": lowest_cap})
    elif over:
        facts.append({"kind": "over_budget_count", "count": over})      # a count only, never names
    popularity: dict = {}
    for wanted in wants.values():
        for feature in wanted:
            popularity[feature] = popularity.get(feature, 0) + 1
    matched = sorted((f for f in popularity if f in features), key=lambda f: (-popularity[f], f))
    for feature in matched[:2]:
        facts.append({"kind": "must_have_count", "feature": feature, "count": popularity[feature]})
    meta = listing.meta or {}
    if meta.get("drive_min") is not None:
        facts.append({"kind": "drive_min", "to": meta.get("to", ""), "minutes": meta["drive_min"]})
    missing = sorted((f for f in popularity if f not in features), key=lambda f: (-popularity[f], f))
    if missing:
        facts.append({"kind": "tradeoff", "text": f"No {missing[0].replace('_', ' ')} (wanted by {popularity[missing[0]]})"})
    elif meta.get("tradeoff"):
        facts.append({"kind": "tradeoff", "text": meta["tradeoff"]})
    return facts


# ---------------------------------------------------------------------------------------------------
# Votes
# ---------------------------------------------------------------------------------------------------
def quorum_for(active_count: int) -> int:
    """More than half of the active members."""
    return active_count // 2 + 1


def close_if_decided(vote: Vote) -> Vote:
    """
    Close the vote when time is up, everybody has voted, or one option already has a majority of the voters
    (nobody can catch up after that, so waiting would only slow the group down). The winner needs quorum (more than half of the
    group voted) and strictly more votes than the runner-up. A tie closes the vote with no winner.
    """
    if vote.status != Vote.Status.OPEN:
        return vote
    from purchases.models import TripPlan
    plan = TripPlan.objects.filter(vote=vote).first()
    if plan and len(active_members(vote.group_id)) < plan.expected_members and timezone.now() < vote.closes_at:
        return vote
    eligible = eligible_voters(vote)
    ballots = Ballot.objects.filter(vote=vote, member__in=eligible)
    voted = ballots.count()
    everyone = voted >= len(eligible)
    counts = sorted(ballots.values("choice_key").annotate(n=Count("id")).values_list("choice_key", "n"),
                    key=lambda kv: -kv[1])
    majority = bool(counts) and counts[0][1] >= vote.quorum        # more than half already agree: nobody can overtake them
    if not (everyone or majority or timezone.now() >= vote.closes_at):
        return vote
    winner = ""
    if voted >= vote.quorum and counts and (len(counts) == 1 or counts[0][1] > counts[1][1]):
        winner = counts[0][0]
    vote.status, vote.winner_key = Vote.Status.CLOSED, winner
    vote.save(update_fields=["status", "winner_key", "updated_at"])
    log_event(EventType.VOTE_CLOSED, actor="rules", group_id=vote.group_id,
              payload={"vote_id": str(vote.id), "winner_key": winner, "voted": voted})
    return vote
