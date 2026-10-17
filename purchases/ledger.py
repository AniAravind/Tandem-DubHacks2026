"""
The shared-expense ledger (milestone M5).

Everything here is whole cents (integers), never dollars with decimals, so nothing is ever off by a rounding error.

The three ways to split an expense:
  equal   everybody listed pays the same.             $60.00 between 5 people -> 5 x $12.00
  shares  people pay in proportion to their shares.   A member listed twice in member_ids has 2 shares.
  custom  you say exactly how much each person owes.  The amounts must add up to the expense.

When a split does not divide evenly, the leftover cents go to the FIRST people in the group's stable member order
(the order they joined), one cent each. So $549.00 between 8 people is 4 x $68.63 and 4 x $68.62 (which adds up).

Balances: a positive number means "the group owes this person", a negative number means "this person owes the group".
All balances always add up to exactly 0.
"""
from __future__ import annotations

from chat.adapters.base import AgentMessage, Card
from core.events import EventType, log_event
from core.models import Group, Member
from django.db import transaction
from purchases.models import Expense, ExpenseSplit

MAX_EXPENSE_CENTS = 500_000            # $5,000, same limit as the add_expense tool
MAX_DESCRIPTION = 80
MODES = ("equal", "shares", "custom")


class LedgerError(Exception):
    """A bad expense request. `code` is the machine-readable reason (for example split_mismatch)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ---------------------------------------------------------------- the pure math (no database)
def split_by_weights(total_cents: int, weights: list[int]) -> list[int]:
    """Split `total_cents` in proportion to `weights`. Leftover cents go to the first entries, one each.

    split_by_weights(6000, [1, 1, 1, 1, 1]) -> [1200, 1200, 1200, 1200, 1200]
    split_by_weights(54900, [1] * 8)        -> [6863, 6863, 6863, 6863, 6862, 6862, 6862, 6862]
    """
    weight_sum = sum(weights)
    parts = [total_cents * w // weight_sum for w in weights]          # round everyone DOWN first
    leftover = total_cents - sum(parts)                               # 0 up to len(weights) - 1 cents remain
    for i in range(leftover):
        parts[i] += 1                                                 # hand them out from the front
    return parts


def settle_up(balances: list[dict]) -> list[dict]:
    """Greedy settle-up: the person who owes the most pays the person who is owed the most, repeat.

    Takes the "balances" list from the ledger and returns [{"from", "to", "amount_cents"}, ...] with member ids.
    It never needs more payments than (people - 1).
    """
    owed = sorted(([b["net_cents"], b["member_id"]] for b in balances if b["net_cents"] > 0), key=lambda x: -x[0])
    owes = sorted(([-b["net_cents"], b["member_id"]] for b in balances if b["net_cents"] < 0), key=lambda x: -x[0])
    payments = []
    while owed and owes:
        pay = min(owed[0][0], owes[0][0])
        payments.append({"from": owes[0][1], "to": owed[0][1], "amount_cents": pay})
        owed[0][0] -= pay
        owes[0][0] -= pay
        owed = sorted((o for o in owed if o[0] > 0), key=lambda x: -x[0])
        owes = sorted((o for o in owes if o[0] > 0), key=lambda x: -x[0])
    return payments


# ---------------------------------------------------------------- working out who pays what
def compute_splits(total_cents: int, mode: str, members: list[Member], member_ids: list[str] | None,
                   custom_cents: dict[str, int] | None) -> list[tuple[Member, int]]:
    """Returns [(member, cents), ...] in the group's stable order. Raises LedgerError if the request makes no sense.

    `members` is every ACTIVE member of the group in stable order (oldest first).
    """
    by_id = {str(m.id): m for m in members}
    if mode not in MODES:
        raise LedgerError("invalid_request", f"split mode must be one of {', '.join(MODES)}.")

    if mode == "custom":
        custom_cents = custom_cents or {}
        if not custom_cents:
            raise LedgerError("invalid_request", "A custom split needs custom amounts.")
        for mid, cents in custom_cents.items():
            if mid not in by_id:
                raise LedgerError("not_a_member", "Everyone in the split must be an active member of the group.")
            if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0:
                raise LedgerError("invalid_request", "Custom amounts must be whole cents, zero or more.")
        if sum(custom_cents.values()) != total_cents:
            raise LedgerError("split_mismatch", f"The custom amounts add up to {sum(custom_cents.values())} cents "
                                                f"but the expense is {total_cents} cents.")
        return [(m, custom_cents[str(m.id)]) for m in members if str(m.id) in custom_cents]

    # equal and shares: who is in? Nobody listed means the whole group.
    listed = list(member_ids) if member_ids else [str(m.id) for m in members]
    for mid in listed:
        if mid not in by_id:
            raise LedgerError("not_a_member", "Everyone in the split must be an active member of the group.")
    if mode == "equal":
        weights = {mid: 1 for mid in dict.fromkeys(listed)}                  # duplicates ignored
    else:
        weights = {}
        for mid in listed:                                                   # "shares": each mention is one share
            weights[mid] = weights.get(mid, 0) + 1
    people = [m for m in members if str(m.id) in weights]                    # stable order, not the order sent
    cents = split_by_weights(total_cents, [weights[str(m.id)] for m in people])
    return list(zip(people, cents))


# ---------------------------------------------------------------- writing an expense
@transaction.atomic
def add_expense(group: Group, payer_id: str, amount_cents: int, description: str, mode: str,
                member_ids: list[str] | None = None, custom_cents: dict[str, int] | None = None,
                purchase=None, actor: str = "rules") -> dict:
    """Validate, split, save the Expense and its ExpenseSplit rows, log it, and post an ExpenseCard in the chat."""
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or not 0 < amount_cents <= MAX_EXPENSE_CENTS:
        raise LedgerError("invalid_request", "The amount must be more than $0 and at most $5,000.")
    description = (description or "Expense").strip() or "Expense"
    if len(description) > MAX_DESCRIPTION:
        raise LedgerError("invalid_request", f"The description can be at most {MAX_DESCRIPTION} characters.")

    members = list(Member.objects.filter(group=group, status=Member.Status.ACTIVE).order_by("created_at", "display_name"))
    payer = next((m for m in members if str(m.id) == str(payer_id)), None)
    if payer is None:
        raise LedgerError("not_a_member", "The payer must be an active member of the group.")

    splits = compute_splits(amount_cents, mode, members, member_ids, custom_cents)
    if sum(cents for _, cents in splits) != amount_cents:                     # belt and braces: should never happen
        raise LedgerError("split_mismatch", "The split does not add up to the expense.")

    expense = Expense.objects.create(group=group, purchase=purchase, payer=payer, amount_cents=amount_cents,
                                     description=description, split_mode=mode)
    ExpenseSplit.objects.bulk_create([ExpenseSplit(expense=expense, member=m, amount_cents=c) for m, c in splits])
    per_person = max(c for _, c in splits)
    log_event(EventType.EXPENSE_ADDED, actor=actor, group_id=group.id,
              payload={"expense_id": str(expense.id), "payer_id": str(payer.id), "amount_cents": amount_cents,
                       "split_mode": mode, "split_count": len(splits)})
    from purchases.rules import _post                                         # imported here to avoid a circular import
    _post(group.id, AgentMessage(
        text=f"{payer.display_name} paid ${amount_cents / 100:,.2f} for {description}.",
        card=Card("ExpenseCard", {"expense_id": str(expense.id), "description": description,
                                  "payer": payer.display_name, "amount_cents": amount_cents,
                                  "split_count": len(splits), "per_person_cents": per_person})))
    return {"expense_id": str(expense.id), "description": description, "amount_cents": amount_cents,
            "per_person_cents": per_person, "split_count": len(splits),
            "splits": [{"member_id": str(m.id), "amount_cents": c} for m, c in splits],
            "balances_net_cents": sum(b["net_cents"] for b in compute_balances(group))}


# ---------------------------------------------------------------- reading the ledger
def compute_balances(group: Group) -> list[dict]:
    """[{"member_id", "display_name", "net_cents"}] biggest creditor first. Always sums to 0."""
    members = {m.id: m for m in group.members.all()}
    net = {mid: 0 for mid in members}
    for e in Expense.objects.filter(group=group).prefetch_related("splits"):
        net[e.payer_id] = net.get(e.payer_id, 0) + e.amount_cents            # what they paid out
        for sp in e.splits.all():
            net[sp.member_id] = net.get(sp.member_id, 0) - sp.amount_cents   # what they owe
    return [{"member_id": str(mid), "display_name": members[mid].display_name, "net_cents": cents}
            for mid, cents in sorted(net.items(), key=lambda kv: (-kv[1], members[kv[0]].display_name)) if mid in members]
