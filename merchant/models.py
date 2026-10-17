from django.db import models
from core.models import TimeStamped


class Merchant(TimeStamped):
    slug = models.SlugField(unique=True)                    # pinestay | summitpass | couchco | gamehub
    name = models.CharField(max_length=80)
    category = models.CharField(max_length=24)
    mcc = models.CharField(max_length=4, blank=True)        # demo value


class Listing(TimeStamped):
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="listings")
    ref = models.SlugField()                                # "pinecrest-a-frame"
    title = models.CharField(max_length=120)
    total_cents = models.BigIntegerField()                  # per stay / per bundle
    unit_price_cents = models.BigIntegerField(null=True, blank=True)  # per-person items (passes)
    capacity = models.PositiveIntegerField()
    features = models.JSONField(default=list)
    meta = models.JSONField(default=dict)                   # {"demo_data": true, "drive_min": 12, ...}
    available = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["merchant", "ref"], name="uniq_listing_ref")]


class Quote(TimeStamped):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE)
    quote_id = models.CharField(max_length=64, unique=True) # "q_pinestay_88"
    amount_cents = models.BigIntegerField()
    guests = models.PositiveIntegerField()
    check_in = models.DateField(null=True, blank=True)
    check_out = models.DateField(null=True, blank=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)


class SignedRequest(TimeStamped):
    """Audit trail for every TAP-style signed request (written by signer and verifier)."""
    purpose = models.CharField(max_length=16)               # browse | checkout | cancel
    method = models.CharField(max_length=8)
    authority = models.CharField(max_length=255)
    path = models.CharField(max_length=255)
    keyid = models.CharField(max_length=64)
    nonce = models.CharField(max_length=128, db_index=True)
    created = models.BigIntegerField()
    expires = models.BigIntegerField()
    tag = models.CharField(max_length=32)                   # agent-browser-auth | agent-payer-auth
    content_digest = models.CharField(max_length=128, blank=True)
    signature_b64 = models.TextField()
    response_code = models.PositiveSmallIntegerField(null=True)
    verified = models.BooleanField(default=False)
    reject_reason = models.CharField(max_length=32, blank=True)   # see 6.9


class MerchantBooking(TimeStamped):
    listing = models.ForeignKey(Listing, on_delete=models.PROTECT)
    quote = models.OneToOneField(Quote, on_delete=models.PROTECT)
    booking_ref = models.CharField(max_length=16, unique=True)    # "PS-4K7Q"
    amount_cents = models.BigIntegerField()
    status = models.CharField(max_length=16, default="confirmed") # confirmed | cancelled
    idempotency_key = models.CharField(max_length=128, unique=True)
