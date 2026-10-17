import uuid
from django.db import models


class TimeStamped(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Group(TimeStamped):
    class Channel(models.TextChoices):
        WEB = "web"; IMESSAGE = "imessage"

    class Status(models.TextChoices):
        ACTIVE = "active"; ARCHIVED = "archived"

    name = models.CharField(max_length=120)
    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.WEB)
    external_thread_id_hash = models.CharField(max_length=64, blank=True, db_index=True)  # Photon thread id, hashed
    organizer = models.ForeignKey("core.Member", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    timezone = models.CharField(max_length=64, default="America/Los_Angeles")


class Member(TimeStamped):
    class Role(models.TextChoices):
        ORGANIZER = "organizer"; MEMBER = "member"

    class Status(models.TextChoices):
        ACTIVE = "active"; LEFT = "left"; REMOVED = "removed"

    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="members")
    display_name = models.CharField(max_length=60)
    phone_e164_hash = models.CharField(max_length=64)
    phone_last4 = models.CharField(max_length=4, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    prefs = models.JSONField(default=dict, blank=True)          # {"must_haves":[], "dealbreakers":[], "flexibility":"firm"}
    budget_cap_cents_enc = models.TextField(blank=True)         # Fernet(str(cents)); PRIVATE, never sent to the LLM
    passkey_credential_id = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["group", "phone_e164_hash"], name="uniq_member_phone_per_group")]

    @property
    def budget_cap_cents(self) -> int | None:
        from core.crypto import decrypt_int
        return decrypt_int(self.budget_cap_cents_enc)

    @budget_cap_cents.setter
    def budget_cap_cents(self, value: int | None) -> None:
        from core.crypto import encrypt_int
        self.budget_cap_cents_enc = encrypt_int(value)


class InviteLink(TimeStamped):
    class Purpose(models.TextChoices):
        JOIN = "join"; CONSENT = "consent"

    group = models.ForeignKey(Group, on_delete=models.CASCADE)
    member = models.ForeignKey(Member, null=True, blank=True, on_delete=models.CASCADE)
    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    token_hash = models.CharField(max_length=64, unique=True)   # sha256 of the signed token
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)


class EventLog(TimeStamped):
    """Append-only, hash-chained. Write ONLY via core.events.log_event()."""
    group = models.ForeignKey(Group, null=True, blank=True, on_delete=models.CASCADE, related_name="events")
    purchase_id = models.UUIDField(null=True, blank=True, db_index=True)
    actor = models.CharField(max_length=64)                     # "member:<uuid>" | agent | rules | gateway | merchant | signer | scheduler | demo
    type = models.CharField(max_length=64, db_index=True)       # core.events.EventType
    payload = models.JSONField(default=dict)                    # redacted: no PAN, no budgets
    simulated = models.BooleanField(default=False)
    seq = models.BigIntegerField(db_index=True)
    prev_hash = models.CharField(max_length=64, blank=True)
    hash = models.CharField(max_length=64)

    class Meta:
        ordering = ["seq"]
