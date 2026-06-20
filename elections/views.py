import csv, io
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import user_passes_test
from django.db import transaction
from django.contrib import messages
from django.http import JsonResponse
from django.utils import timezone
from django.urls import reverse
from .models import Election, Voter, Position, Candidate, Vote, AuditLog
from .forms import VoterLoginForm, VoteForm, ElectionForm, PositionForm, CandidateForm, VoterUploadForm
from .utils import request_ip


def is_admin(user):
    return user.is_authenticated and user.is_staff


def home(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("elections:admin_dashboard")
    vid = request.session.get("voter_id")
    if vid:
        try:
            v = Voter.objects.get(id=vid)
            if not v.has_voted:
                return redirect("elections:ballot", election_id=v.election.id)
        except Voter.DoesNotExist:
            request.session.pop("voter_id", None)
    return render(request, "elections/home.html")


def voter_login_view(request):
    vid = request.session.get("voter_id")
    if vid:
        try:
            v = Voter.objects.get(id=vid)
            if not v.has_voted:
                return redirect("elections:ballot", election_id=v.election.id)
        except Voter.DoesNotExist:
            request.session.pop("voter_id", None)

    if request.method == "POST":
        form = VoterLoginForm(request.POST)
        if form.is_valid():
            voter_id = form.cleaned_data["voter_id"].strip()
            token = form.cleaned_data["token"].strip()
            try:
                voter = Voter.objects.get(token=token, voter_id=voter_id)
                if voter.has_voted:
                    messages.error(request, "You have already cast your vote. This token is used.")
                    return redirect("elections:voter_login")
                request.session["voter_id"] = str(voter.id)
                return redirect("elections:ballot", election_id=voter.election.id)
            except Voter.DoesNotExist:
                messages.error(request, "Invalid voter ID or token. Please check and try again.")
                return redirect("elections:voter_login")
    else:
        form = VoterLoginForm()
    return render(request, "elections/voter_login.html", {"form": form})


def admin_login_view(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("elections:admin_dashboard")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip()
        user = authenticate(request, username=username, password=password)
        if user and user.is_staff:
            login(request, user)
            AuditLog.objects.create(user=user, action="admin_login", details={"username": username})
            next_url = request.GET.get("next")
            if next_url:
                return redirect(next_url)
            return redirect("elections:admin_dashboard")
        else:
            messages.error(request, "Invalid admin credentials.")
            return redirect("elections:admin_login")
    return render(request, "elections/admin_login.html")


def logout_view(request):
    request.session.pop("voter_id", None)
    logout(request)
    return redirect("elections:home")


def election_list(request):
    elections = Election.objects.filter(is_published=True).order_by("-start_at")
    return render(request, "elections/election_list.html", {"elections": elections})


def election_detail(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    return render(request, "elections/election_detail.html", {
        "election": election, "open": election.is_open(),
        "total_voters": election.total_voters(), "voted_count": election.voted_count(),
    })


def ballot(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    if not election.is_open():
        messages.error(request, "This election is not open for voting.")
        return redirect("elections:detail", election_id=election.id)
    vid = request.session.get("voter_id")
    if not vid:
        messages.error(request, "Please log in first.")
        return redirect("elections:voter_login")
    voter = get_object_or_404(Voter, id=vid, election=election)
    if voter.has_voted:
        messages.error(request, "You have already voted.")
        return redirect("elections:home")
    positions = election.positions.prefetch_related("candidates")
    if request.method == "POST":
        form = VoteForm(positions, request.POST)
        if form.is_valid():
            selections = {}
            for name, cid in form.cleaned_data.items():
                selections[name.replace("position_", "")] = cid
            with transaction.atomic():
                v = Voter.objects.select_for_update().get(pk=voter.pk)
                if v.has_voted:
                    messages.error(request, "You have already voted.")
                    return redirect("elections:home")
                Vote.objects.create(election=election, voter=v, selections=selections, ip_address=request_ip(request))
                v.has_voted = True; v.voted_at = timezone.now(); v.save(update_fields=["has_voted", "voted_at"])
                AuditLog.objects.create(action="vote_cast", details={"voter": str(v.id), "election": str(election.id)})
            request.session.pop("voter_id", None)
            return redirect("elections:vote_confirmation", election_id=election.id)
        else:
            messages.error(request, "Please select a candidate for each position.")
    else:
        form = VoteForm(positions)
    return render(request, "elections/ballot.html", {"form": form, "election": election, "positions": positions})


def vote_confirmation(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    return render(request, "elections/vote_confirmation.html", {"election": election})


def results(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    election._live = True
    positions = election.positions.prefetch_related("candidates")
    all_votes = list(election.votes.all())
    data = []
    for pos in positions:
        pk = str(pos.id)
        counts = {str(c.id): 0 for c in pos.candidates.all()}
        for vote in all_votes:
            s = vote.selections.get(pk)
            if s in counts:
                counts[s] += 1
        total = sum(counts.values())
        sorted_candidates = sorted(
            [{"name": c.full_name, "photo": c.photo.url if c.photo else None,
              "count": counts.get(str(c.id), 0), "id": str(c.id)} for c in pos.candidates.all()],
            key=lambda x: x["count"], reverse=True,
        )
        data.append({"position": pos, "candidates": sorted_candidates, "total": total})
    return render(request, "elections/results.html", {"election": election, "results_data": data})


def api_results(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    positions = election.positions.prefetch_related("candidates")
    all_votes = list(election.votes.all())
    data = []
    for pos in positions:
        pk = str(pos.id)
        counts = {str(c.id): 0 for c in pos.candidates.all()}
        for vote in all_votes:
            s = vote.selections.get(pk)
            if s in counts:
                counts[s] += 1
        total = sum(counts.values())
        sorted_candidates = sorted(
            [{"name": c.full_name, "count": counts.get(str(c.id), 0), "id": str(c.id)} for c in pos.candidates.all()],
            key=lambda x: x["count"], reverse=True,
        )
        data.append({"position_id": str(pos.id), "title": pos.title, "candidates": sorted_candidates, "total": total})
    return JsonResponse({"election_id": str(election.id), "election_title": election.title, "positions": data})


@user_passes_test(is_admin, login_url="elections:admin_login")
def api_voters(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    voters = election.voters.all().order_by("-created_at")
    data = [{
        "id": str(v.id),
        "voter_id": v.voter_id,
        "full_name": v.full_name or "",
        "email": v.email or "",
        "token": v.token,
        "has_voted": v.has_voted,
        "voted_at": v.voted_at.isoformat() if v.voted_at else None,
    } for v in voters]
    return JsonResponse({
        "election_id": str(election.id),
        "voters": data,
        "voted_count": election.voted_count(),
        "total_count": election.total_voters(),
    })


# ─── Custom Admin Views ─────────────────────────────────────

@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_dashboard(request):
    elections = Election.objects.all().order_by("-created_at")
    tv = sum(e.total_voters() for e in elections)
    tc = sum(e.voted_count() for e in elections)
    logs = AuditLog.objects.all()[:20]
    return render(request, "elections/admin/dashboard.html", {
        "elections": elections, "total_voters": tv, "total_votes": tc, "logs": logs, "now": timezone.now(),
    })


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_election_new(request):
    if request.method == "POST":
        form = ElectionForm(request.POST)
        if form.is_valid():
            e = form.save(commit=False)
            e.created_by = request.user
            e.save()
            AuditLog.objects.create(user=request.user, action="election_created", details={"id": str(e.id), "title": e.title})
            messages.success(request, f"Election '{e.title}' created.")
            return redirect("elections:admin_dashboard")
    else:
        form = ElectionForm(initial={"start_at": timezone.now(), "end_at": timezone.now()})
    return render(request, "elections/admin/election_form.html", {"form": form, "title": "New Election"})


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_election_edit(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    if request.method == "POST":
        form = ElectionForm(request.POST, instance=election)
        if form.is_valid():
            form.save()
            AuditLog.objects.create(user=request.user, action="election_updated", details={"id": str(election.id)})
            messages.success(request, "Election updated.")
            return redirect("elections:admin_election_detail", election_id=election.id)
    else:
        form = ElectionForm(instance=election)
    return render(request, "elections/admin/election_form.html", {"form": form, "title": f"Edit: {election.title}", "election": election})


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_election_detail(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    positions = election.positions.prefetch_related("candidates").all()
    voters = election.voters.all().order_by("-created_at")
    return render(request, "elections/admin/election_detail.html", {
        "election": election, "positions": positions, "voters": voters, "now": timezone.now(),
    })


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_election_delete(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    if request.method == "POST":
        title = election.title
        election.delete()
        AuditLog.objects.create(user=request.user, action="election_deleted", details={"title": title})
        messages.success(request, f"Election '{title}' deleted.")
        return redirect("elections:admin_dashboard")
    return render(request, "elections/admin/confirm_delete.html", {"obj": election, "type": "election"})


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_election_toggle(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    election.is_published = not election.is_published
    election.save(update_fields=["is_published"])
    status = "published" if election.is_published else "unpublished"
    AuditLog.objects.create(user=request.user, action=f"election_{status}", details={"id": str(election.id)})
    messages.success(request, f"Election '{election.title}' {status}.")
    return redirect("elections:admin_election_detail", election_id=election.id)


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_position_new(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    if request.method == "POST":
        form = PositionForm(request.POST)
        if form.is_valid():
            p = form.save(commit=False)
            p.election = election
            p.save()
            messages.success(request, f"Position '{p.title}' added.")
            return redirect("elections:admin_election_detail", election_id=election.id)
    else:
        form = PositionForm()
    return render(request, "elections/admin/position_form.html", {"form": form, "election": election, "title": "New Position"})


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_position_edit(request, election_id, position_id):
    position = get_object_or_404(Position, id=position_id, election_id=election_id)
    if request.method == "POST":
        form = PositionForm(request.POST, instance=position)
        if form.is_valid():
            form.save()
            messages.success(request, "Position updated.")
            return redirect("elections:admin_election_detail", election_id=election_id)
    else:
        form = PositionForm(instance=position)
    return render(request, "elections/admin/position_form.html", {"form": form, "election": position.election, "title": f"Edit: {position.title}"})


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_position_delete(request, election_id, position_id):
    position = get_object_or_404(Position, id=position_id, election_id=election_id)
    if request.method == "POST":
        position.delete()
        messages.success(request, "Position deleted.")
        return redirect("elections:admin_election_detail", election_id=election_id)
    return render(request, "elections/admin/confirm_delete.html", {"obj": position, "type": "position"})


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_candidate_new(request, election_id, position_id):
    position = get_object_or_404(Position, id=position_id, election_id=election_id)
    if request.method == "POST":
        form = CandidateForm(request.POST, request.FILES)
        if form.is_valid():
            c = form.save(commit=False)
            c.position = position
            c.save()
            messages.success(request, f"Candidate '{c.full_name}' added.")
            return redirect("elections:admin_election_detail", election_id=election_id)
    else:
        form = CandidateForm()
    return render(request, "elections/admin/candidate_form.html", {"form": form, "position": position, "election": position.election, "title": "New Candidate"})


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_candidate_edit(request, election_id, position_id, candidate_id):
    candidate = get_object_or_404(Candidate, id=candidate_id, position_id=position_id, position__election_id=election_id)
    if request.method == "POST":
        form = CandidateForm(request.POST, request.FILES, instance=candidate)
        if form.is_valid():
            form.save()
            messages.success(request, "Candidate updated.")
            return redirect("elections:admin_election_detail", election_id=election_id)
    else:
        form = CandidateForm(instance=candidate)
    return render(request, "elections/admin/candidate_form.html", {"form": form, "position": candidate.position, "election": candidate.position.election, "title": f"Edit: {candidate.full_name}"})


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_candidate_delete(request, election_id, position_id, candidate_id):
    candidate = get_object_or_404(Candidate, id=candidate_id, position_id=position_id, position__election_id=election_id)
    if request.method == "POST":
        candidate.delete()
        messages.success(request, "Candidate deleted.")
        return redirect("elections:admin_election_detail", election_id=election_id)
    return render(request, "elections/admin/confirm_delete.html", {"obj": candidate, "type": "candidate"})


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_voters(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    voters = election.voters.all().order_by("-created_at")
    return render(request, "elections/admin/voters.html", {
        "election": election, "voters": voters,
        "voted_count": election.voted_count(),
        "pending_count": election.total_voters() - election.voted_count(),
    })


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_voters_upload(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    if request.method == "POST":
        form = VoterUploadForm(request.POST, request.FILES)
        if form.is_valid():
            file = request.FILES["csv_file"]
            decoded = file.read().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(decoded))
            created = 0; errors = 0
            for row in reader:
                try:
                    Voter.objects.create(
                        election=election,
                        voter_id=row.get("voter_id", "").strip(),
                        full_name=row.get("full_name", "").strip(),
                        email=row.get("email", "").strip() or None,
                    )
                    created += 1
                except Exception:
                    errors += 1
            AuditLog.objects.create(user=request.user, action="voters_imported", details={"election": str(election.id), "created": created, "errors": errors})
            messages.success(request, f"Imported {created} voter(s). Errors: {errors}")
            return redirect("elections:admin_voters", election_id=election.id)
    else:
        form = VoterUploadForm()
    return render(request, "elections/admin/voters_upload.html", {"form": form, "election": election})


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_voter_reset(request, election_id, voter_id):
    election = get_object_or_404(Election, id=election_id)
    voter = get_object_or_404(Voter, id=voter_id, election_id=election_id)
    if request.method == "POST":
        Vote.objects.filter(voter=voter).delete()
        voter.has_voted = False
        voter.voted_at = None
        voter.save(update_fields=["has_voted", "voted_at"])
        AuditLog.objects.create(user=request.user, action="voter_reset", details={"voter": str(voter.id)})
        messages.success(request, f"Voter '{voter.voter_id}' can vote again.")
        return redirect("elections:admin_voters", election_id=election.id)
    return render(request, "elections/admin/confirm_delete.html", {"obj": voter, "type": "voter reset", "action": "reset"})


@user_passes_test(is_admin, login_url="elections:admin_login")
def admin_voter_regenerate_token(request, election_id, voter_id):
    election = get_object_or_404(Election, id=election_id)
    voter = get_object_or_404(Voter, id=voter_id, election_id=election_id)
    from django.utils.crypto import get_random_string
    voter.token = get_random_string(6)
    voter.save(update_fields=["token"])
    AuditLog.objects.create(user=request.user, action="token_regenerated", details={"voter": str(voter.id)})
    messages.success(request, f"New token for '{voter.voter_id}': {voter.token}")
    return redirect("elections:admin_voters", election_id=election.id)
