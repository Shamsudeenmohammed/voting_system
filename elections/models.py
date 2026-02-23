# elections/models.py
import uuid
import hashlib
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

def compute_record_hash(payload_json: str, previous_hash: str | None) -> str:
    # deterministic hash: sha256(payload || previous_hash or '')
    hasher = hashlib.sha256()
    hasher.update(payload_json.encode("utf-8"))
    hasher.update((previous_hash or "").encode("utf-8"))
    return hasher.hexdigest()

class Election(models.Model):
    BALLOT_CHOICES = [
        ("single", "Single choice"),
        ("multiple", "Multiple choice"),
        ("ranked", "Ranked choice (IRV)"),
        ("approval", "Approval"),
        ("score", "Score"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    ballot_type = models.CharField(max_length=32, choices=BALLOT_CHOICES, default="single")
    is_published = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_open(self):
        now = timezone.now()
        return self.is_published and self.start_at <= now <= self.end_at

    def __str__(self):
        return self.title

class Ballot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(Election, related_name="ballots", on_delete=models.CASCADE)
    question = models.TextField()
    order = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.election.title} — Q{self.order}"

# elections/models.py (add/update these)

from django.utils.crypto import get_random_string

class Voter(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(Election, related_name="voters", on_delete=models.CASCADE)
    student_id = models.CharField(max_length=64, null=True, blank=True)  # optional for student elections
    external_id = models.CharField(max_length=255, null=True, blank=True) # voter_id
    email = models.EmailField(null=True, blank=True)
    phone = models.CharField(max_length=32, null=True, blank=True)
    token = models.CharField(max_length=128, unique=True, blank=True)  # for login
    has_voted = models.BooleanField(default=False)
    invited_at = models.DateTimeField(null=True, blank=True)
    verified = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        if not self.token:
            # generate a random secure token/password for login
            self.token = get_random_string(12)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.student_id or self.external_id or str(self.id)


class Choice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ballot = models.ForeignKey(Ballot, related_name="choices", on_delete=models.CASCADE)
    text = models.CharField(max_length=512)
    order = models.IntegerField(default=0)
    image = models.ImageField(upload_to='candidates/', null=True, blank=True)  # candidate photo

    def __str__(self):
        return self.text


class VoteRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(Election, on_delete=models.CASCADE)
    voter = models.ForeignKey(Voter, on_delete=models.SET_NULL, null=True, blank=True)
    payload = models.JSONField()  # store votes in structured JSON
    created_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    record_hash = models.CharField(max_length=128, null=True, blank=True)  # sha256 payload + prev hash

    class Meta:
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        # compute record_hash before save if not present
        if not self.record_hash:
            # find previous hash for this election
            prev = VoteRecord.objects.filter(election=self.election).order_by("-created_at").first()
            prev_hash = prev.record_hash if prev else ""
            import json
            payload_json = json.dumps(self.payload, sort_keys=True, separators=(",", ":"))
            self.record_hash = compute_record_hash(payload_json, prev_hash)
        super().save(*args, **kwargs)

class AuditLog(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=255)
    details = models.JSONField(null=True, blank=True)

    def __str__(self):
        return f"{self.timestamp} — {self.action}"
