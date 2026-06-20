import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.crypto import get_random_string


class Election(models.Model):
    CATEGORY_CHOICES = [
        ("general", "General Election"),
        ("tertiary", "Tertiary / University"),
        ("senior_high", "Senior High School"),
        ("primary", "Primary / Junior High"),
        ("local", "Local / Community"),
        ("corporate", "Corporate / Organization"),
    ]
    TYPE_CHOICES = [
        ("single", "Single choice"),
        ("multiple", "Multiple choice"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES, default="general")
    ballot_type = models.CharField(max_length=32, choices=TYPE_CHOICES, default="single")
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    is_published = models.BooleanField(default=False)
    show_results = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def is_open(self):
        now = timezone.now()
        return self.is_published and self.start_at <= now <= self.end_at

    def total_voters(self):
        return self.voters.count()

    def voted_count(self):
        return self.voters.filter(has_voted=True).count()

    def __str__(self):
        return self.title


class Position(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(Election, related_name="positions", on_delete=models.CASCADE)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.election.title} — {self.title}"


class Candidate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    position = models.ForeignKey(Position, related_name="candidates", on_delete=models.CASCADE)
    full_name = models.CharField(max_length=255)
    bio = models.TextField(blank=True)
    photo = models.ImageField(upload_to="candidates/", null=True, blank=True)
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return self.full_name


class Voter(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(Election, related_name="voters", on_delete=models.CASCADE)
    voter_id = models.CharField(max_length=255, verbose_name="Voter ID")
    full_name = models.CharField(max_length=255, blank=True)
    email = models.EmailField(null=True, blank=True)
    token = models.CharField(max_length=128, unique=True, blank=True)
    has_voted = models.BooleanField(default=False)
    voted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = get_random_string(6)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.full_name or self.voter_id


class Vote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(Election, related_name="votes", on_delete=models.CASCADE)
    voter = models.OneToOneField(Voter, on_delete=models.CASCADE)
    selections = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    def __str__(self):
        return f"{self.voter} — {self.election}"


class AuditLog(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=255)
    details = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.timestamp} — {self.action}"
