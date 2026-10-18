from django.db import models
from core.models import TimeStamped, Group, Member


class ChatMessage(TimeStamped):
    class Direction(models.TextChoices):
        INBOUND = "in"; OUTBOUND = "out"

    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="messages")
    member = models.ForeignKey(Member, null=True, blank=True, on_delete=models.SET_NULL)  # null = Tandem
    direction = models.CharField(max_length=3, choices=Direction.choices)
    text_redacted = models.TextField()                      # phone/email/PAN patterns stripped
    card = models.JSONField(null=True, blank=True)          # AgentMessage.card (6.6)
    quick_replies = models.JSONField(default=list, blank=True)
    channel = models.CharField(max_length=16, default="web")
    seq = models.BigIntegerField(db_index=True)             # per-group ordering for polling


class PhotonSession(models.Model):
    """One isolated direct-message agent; no shared demo identity or group routing."""
    key = models.CharField(max_length=64, primary_key=True)
    member = models.OneToOneField(Member, on_delete=models.CASCADE)
    delivered_seq = models.BigIntegerField(default=0)
    planning_state = models.JSONField(default=dict, blank=True)


class PhotonReceipt(models.Model):
    external_id = models.CharField(max_length=64, primary_key=True)
    message = models.OneToOneField(ChatMessage, on_delete=models.CASCADE)
