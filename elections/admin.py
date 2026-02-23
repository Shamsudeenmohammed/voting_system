# elections/admin.py
from django.contrib import admin
from .models import Election, Ballot, Choice, Voter, VoteRecord, AuditLog

class ChoiceInline(admin.TabularInline):
    model = Choice
    extra = 1

class BallotInline(admin.StackedInline):
    model = Ballot
    extra = 0

@admin.register(Election)
class ElectionAdmin(admin.ModelAdmin):
    list_display = ("title", "start_at", "end_at", "ballot_type", "is_published")
    inlines = [BallotInline]

@admin.register(Ballot)
class BallotAdmin(admin.ModelAdmin):
    list_display = ("question", "election", "order")
    inlines = [ChoiceInline]

@admin.register(Voter)
class VoterAdmin(admin.ModelAdmin):
    list_display = ("external_id", "email", "election", "has_voted", "verified")
    list_filter = ("election", "has_voted")

@admin.register(VoteRecord)
class VoteRecordAdmin(admin.ModelAdmin):
    list_display = ("id", "election", "voter", "created_at")
    readonly_fields = ("payload", "record_hash")

@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "action", "user")
