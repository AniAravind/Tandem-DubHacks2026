from django.db import models
from core.models import TimeStamped, Member


class AgentToken(TimeStamped):
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="agent_tokens")
    gateway_token_id = models.CharField(max_length=64, unique=True)   # "atk_sim_..."
    card_brand = models.CharField(max_length=16)                      # "visa"
    card_last4 = models.CharField(max_length=4)                       # NEVER store a PAN
    scope = models.JSONField()                                        # {"purchase_id","merchant_slugs","max_amount_cents"}
    expires_at = models.DateTimeField()
    status = models.CharField(max_length=16, default="active")        # active | revoked | expired


class Consent(TimeStamped):
    """Immutable record of exactly what the member approved."""
    class Method(models.TextChoices):
        PASSKEY = "passkey"; OTP = "otp"

    member = models.ForeignKey(Member, on_delete=models.PROTECT)
    share = models.ForeignKey("purchases.Share", on_delete=models.PROTECT, related_name="consents")
    consent_text_version = models.CharField(max_length=16)            # "v1"
    consent_text_sha256 = models.CharField(max_length=64)
    amount_cents = models.BigIntegerField()
    cap_cents = models.BigIntegerField()
    method = models.CharField(max_length=8, choices=Method.choices)
    ip_hash = models.CharField(max_length=64, blank=True)
    user_agent_hash = models.CharField(max_length=64, blank=True)
    accepted_at = models.DateTimeField()


class Mandate(TimeStamped):
    class Status(models.TextChoices):
        ACTIVE = "active"; REVOKED = "revoked"; EXPIRED = "expired"; CONSUMED = "consumed"

    share = models.ForeignKey("purchases.Share", on_delete=models.PROTECT, related_name="mandates")
    member = models.ForeignKey(Member, on_delete=models.PROTECT)
    agent_token = models.ForeignKey(AgentToken, on_delete=models.PROTECT)
    consent = models.OneToOneField(Consent, on_delete=models.PROTECT)
    merchant_slug = models.CharField(max_length=40)                   # merchant lock
    listing_ref = models.CharField(max_length=80)                     # listing lock
    cap_cents = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    expires_at = models.DateTimeField()
    scope = models.JSONField(default=list)                            # ["authorize_hold","capture","release_hold","refund"]
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    gateway_mandate_id = models.CharField(max_length=64, unique=True) # "mdt_sim_..."


class Hold(TimeStamped):
    class Status(models.TextChoices):
        AUTHORIZED = "authorized"; CAPTURED = "captured"; RELEASED = "released"; EXPIRED = "expired"; DECLINED = "declined"

    share = models.ForeignKey("purchases.Share", on_delete=models.PROTECT, related_name="holds")
    mandate = models.ForeignKey(Mandate, on_delete=models.PROTECT)
    gateway_hold_id = models.CharField(max_length=64, unique=True)    # "auth_sim_..."
    gateway_capture_id = models.CharField(max_length=64, blank=True)  # "cap_sim_..."
    amount_cents = models.BigIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices)
    decline_code = models.CharField(max_length=32, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)


class IdempotencyRecord(TimeStamped):
    key = models.CharField(max_length=128, unique=True)
    request_hash = models.CharField(max_length=64)
    response = models.JSONField()
