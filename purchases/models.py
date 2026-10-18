from django.db import models
from core.models import TimeStamped, Group, Member


class Option(TimeStamped):
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="options")
    merchant_slug = models.CharField(max_length=40)            # "pinestay"
    listing_ref = models.CharField(max_length=80)              # "pinecrest-a-frame"
    category = models.CharField(max_length=24)                 # rental | passes | flights | furniture | electronics | gift_bundle
    title = models.CharField(max_length=120)
    total_cents = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    capacity = models.PositiveIntegerField()
    features = models.JSONField(default=list)                  # ["hot_tub","kitchen",...]
    meta = models.JSONField(default=dict)                      # photo_url, drive_min, cancellation, beds
    quote_id = models.CharField(max_length=64, blank=True)
    quote_expires_at = models.DateTimeField(null=True, blank=True)
    score = models.FloatField(null=True, blank=True)
    explanation_facts = models.JSONField(default=list)         # structured facts the LLM may cite


class TripPlan(TimeStamped):
    source_group = models.ForeignKey(Group, on_delete=models.PROTECT, related_name="published_plans")
    group = models.OneToOneField(Group, on_delete=models.CASCADE, related_name="trip_plan")
    selected_option = models.OneToOneField(Option, on_delete=models.PROTECT, null=True, blank=True)
    vote = models.OneToOneField("Vote", on_delete=models.PROTECT, null=True, blank=True)
    title = models.CharField(max_length=120)
    summary = models.TextField()
    expected_members = models.PositiveSmallIntegerField()
    details = models.JSONField(default=dict, blank=True)


class Vote(TimeStamped):
    class Purpose(models.TextChoices):
        PICK_OPTION = "pick_option"; FALLBACK = "fallback"; PRICE_CHANGE = "price_change"; CANCEL = "cancel"

    class Status(models.TextChoices):
        OPEN = "open"; CLOSED = "closed"

    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="votes")
    purchase = models.ForeignKey("purchases.Purchase", null=True, blank=True, on_delete=models.CASCADE)
    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    choices = models.JSONField()                               # [{"key":"1","option_id":"...","label":"..."}] or fallback keys
    closes_at = models.DateTimeField()
    quorum = models.PositiveIntegerField()                     # majority of active members
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    winner_key = models.CharField(max_length=40, blank=True)


class Ballot(TimeStamped):
    vote = models.ForeignKey(Vote, on_delete=models.CASCADE, related_name="ballots")
    member = models.ForeignKey(Member, on_delete=models.CASCADE)
    choice_key = models.CharField(max_length=40)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["vote", "member"], name="one_ballot_per_member")]


class Purchase(TimeStamped):
    class State(models.TextChoices):
        PROPOSED = "proposed"; VOTING = "voting"; COLLECTING = "collecting"; FUNDED = "funded"
        BOOKED = "booked"; COMPLETED = "completed"; FAILED = "failed"; RELEASED = "released"
        CANCELLED = "cancelled"; REFUNDED = "refunded"

    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="purchases")
    option = models.ForeignKey(Option, on_delete=models.PROTECT)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PROPOSED)
    total_cents = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    deadline_at = models.DateTimeField()
    version = models.PositiveIntegerField(default=1)           # bump on any share/option change
    funded_at = models.DateTimeField(null=True, blank=True)
    booked_at = models.DateTimeField(null=True, blank=True)


class Share(TimeStamped):
    class Status(models.TextChoices):
        INVITED = "invited"; VIEWING = "viewing"; HELD = "held"; DECLINED = "declined"
        CARD_FAILED = "card_failed"; RELEASED = "released"; CAPTURED = "captured"; REFUNDED = "refunded"

    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="shares")
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="shares")
    amount_cents = models.BigIntegerField()
    rounding_adj_cents = models.IntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.INVITED)
    purchase_version = models.PositiveIntegerField(default=1)
    superseded_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL)
    card_attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["purchase", "member"], condition=models.Q(superseded_by__isnull=True),
            name="one_active_share_per_member")]


class Booking(TimeStamped):
    class Status(models.TextChoices):
        CONFIRMED = "confirmed"; CANCELLED = "cancelled"

    purchase = models.OneToOneField(Purchase, on_delete=models.PROTECT, related_name="booking")
    merchant_slug = models.CharField(max_length=40)
    merchant_booking_ref = models.CharField(max_length=40)     # "PS-4K7Q"
    amount_cents = models.BigIntegerField()
    signed_request = models.ForeignKey("merchant.SignedRequest", null=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CONFIRMED)
    cancellation_policy = models.JSONField(default=dict)


class Expense(TimeStamped):
    class SplitMode(models.TextChoices):
        EQUAL = "equal"; SHARES = "shares"; CUSTOM = "custom"

    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="expenses")
    purchase = models.ForeignKey(Purchase, null=True, blank=True, on_delete=models.SET_NULL)
    payer = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="paid_expenses")
    amount_cents = models.BigIntegerField()
    description = models.CharField(max_length=80)
    split_mode = models.CharField(max_length=8, choices=SplitMode.choices, default=SplitMode.EQUAL)


class ExpenseSplit(TimeStamped):
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="splits")
    member = models.ForeignKey(Member, on_delete=models.PROTECT)
    amount_cents = models.BigIntegerField()                    # sum over splits == expense.amount_cents (asserted in rules)


class Refund(TimeStamped):
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="refunds")
    share = models.ForeignKey(Share, on_delete=models.PROTECT)
    amount_cents = models.BigIntegerField()
    gateway_refund_id = models.CharField(max_length=64)
    status = models.CharField(max_length=16, default="refunded")


class LiveOffer(TimeStamped):
    """A public web search result. Never a quote: it cannot be voted on, held, or checked out."""
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="live_offers")
    search_id = models.CharField(max_length=32, db_index=True)
    brief_revision = models.CharField(max_length=32, blank=True)
    label = models.CharField(max_length=1)                     # "A".."E", distinct from catalog option numbers
    kind = models.CharField(max_length=16)                     # stay | product | gift | flight | activity | other
    query = models.CharField(max_length=200)
    title = models.CharField(max_length=160)
    url = models.URLField(max_length=500)
    source = models.CharField(max_length=120)                  # domain the result came from
    snippet = models.CharField(max_length=320, blank=True)
    price_cents = models.BigIntegerField(null=True, blank=True)    # only when the amount appears verbatim in the source text
    price_unit = models.CharField(max_length=12, blank=True)       # night | person | each | listed
    price_text = models.CharField(max_length=60, blank=True)       # the exact matched source text, e.g. "$295/night"
    est_per_person_cents = models.BigIntegerField(null=True, blank=True)
