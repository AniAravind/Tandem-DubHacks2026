from django.db import transaction
from django.utils import timezone
from core.events import log_event, EventType
from purchases.models import Purchase, Share

P = Purchase.State
S = Share.Status

PURCHASE_TRANSITIONS: dict[str, set[str]] = {
    P.PROPOSED:   {P.VOTING},
    P.VOTING:     {P.PROPOSED, P.COLLECTING},          # no quorum / tie -> re-vote
    P.COLLECTING: {P.COLLECTING, P.FUNDED, P.FAILED},  # self-loop = fallback applied (version bump)
    P.FUNDED:     {P.BOOKED, P.COLLECTING},            # checkout rejected for price change -> back to vote
    P.BOOKED:     {P.COMPLETED, P.CANCELLED},
    P.CANCELLED:  {P.REFUNDED},
    P.FAILED:     {P.RELEASED},
    P.COMPLETED: set(), P.RELEASED: set(), P.REFUNDED: set(),
}

SHARE_TRANSITIONS: dict[str, set[str]] = {
    S.INVITED:     {S.VIEWING, S.DECLINED, S.RELEASED},
    S.VIEWING:     {S.HELD, S.DECLINED, S.CARD_FAILED, S.RELEASED},
    S.CARD_FAILED: {S.VIEWING, S.DECLINED, S.RELEASED},   # retry allowed up to 3 times (rules.py enforces)
    S.HELD:        {S.CAPTURED, S.RELEASED},
    S.CAPTURED:    {S.REFUNDED},
    S.DECLINED: set(), S.RELEASED: set(), S.REFUNDED: set(),
}


class IllegalTransition(Exception):
    pass


@transaction.atomic
def transition_purchase(purchase_id, to: str, actor: str, **payload) -> Purchase:
    p = Purchase.objects.select_for_update().get(pk=purchase_id)
    if to not in PURCHASE_TRANSITIONS[p.state]:
        raise IllegalTransition(f"purchase {p.state} -> {to}")
    frm, p.state = p.state, to
    if to == P.FUNDED:
        p.funded_at = timezone.now()
    if to == P.BOOKED:
        p.booked_at = timezone.now()
    p.save(update_fields=["state", "funded_at", "booked_at", "updated_at"])
    log_event(EventType.PURCHASE_STATE_CHANGED, actor=actor, group_id=p.group_id, purchase_id=p.id,
              payload={"from": frm, "to": to, **payload})
    return p


@transaction.atomic
def transition_share(share_id, to: str, actor: str, **payload) -> Share:
    s = Share.objects.select_for_update().select_related("purchase").get(pk=share_id)
    if to not in SHARE_TRANSITIONS[s.status]:
        raise IllegalTransition(f"share {s.status} -> {to}")
    frm, s.status = s.status, to
    s.save(update_fields=["status", "updated_at"])
    log_event(EventType.SHARE_STATE_CHANGED, actor=actor, group_id=s.purchase.group_id,
              purchase_id=s.purchase_id, payload={"share_id": str(s.id), "from": frm, "to": to, **payload})
    return s
