# elections/views.py
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.db import transaction
from django.contrib import messages
from django.utils import timezone
from .models import Election, Voter, Ballot, Choice, VoteRecord, AuditLog
from .forms import TokenForm, SingleChoiceForm, MultipleChoiceForm
from .utils import request_ip
import json


# elections/views.py

from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404
from .models import Election, Voter, Ballot
from .forms import VoterLoginForm

def voter_login(request, election_id):
    election = get_object_or_404(Election, id=election_id)

    if request.method == "POST":
        form = VoterLoginForm(request.POST)
        if form.is_valid():
            voter_id = form.cleaned_data["voter_id"].strip()
            token = form.cleaned_data["token"].strip()

            try:
                voter = Voter.objects.get(
                    election=election,
                    token=token,
                    student_id=voter_id
                )
            except Voter.DoesNotExist:
                try:
                    voter = Voter.objects.get(
                        election=election,
                        token=token,
                        external_id=voter_id
                    )
                except Voter.DoesNotExist:
                    messages.error(request, "Invalid voter ID or password.")
                    return redirect("elections:voter_login", election_id=election.id)

            if voter.has_voted:
                messages.error(request, "You have already voted.")
                return redirect("elections:election_detail", election_id=election.id)

            # Store voter in session
            request.session["voter_id"] = str(voter.id)
            return redirect("elections:ballot", election_id=election.id)

    else:
        form = VoterLoginForm()

    return render(request, "elections/voter_login.html", {"form": form, "election": election})


def election_list(request):
    elections = Election.objects.filter(is_published=True).order_by("-start_at")
    return render(request, "elections/election_list.html", {"elections": elections})

def election_detail(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    open_status = election.is_open()
    return render(request, "elections/election_detail.html", {"election": election, "open": open_status})

def start_vote(request, election_id):
    """
    Step 1: Voter enters token (or login), verifies eligibility.
    """
    election = get_object_or_404(Election, id=election_id)
    if not election.is_published:
        messages.error(request, "Election not published.")
        return redirect("elections:list")

    if request.method == "POST":
        form = TokenForm(request.POST)
        if form.is_valid():
            token = form.cleaned_data["token"].strip()
            try:
                voter = Voter.objects.get(election=election, token=token)
            except Voter.DoesNotExist:
                messages.error(request, "Invalid token.")
                return redirect("elections:start_vote", election_id=election.id)

            if voter.has_voted:
                messages.error(request, "This token has already been used to vote.")
                return redirect("elections:start_vote", election_id=election.id)

            # mark verified for session and redirect to ballot view
            request.session["voter_id"] = str(voter.id)
            return redirect("elections:ballot", election_id=election.id)
    else:
        form = TokenForm()
    return render(request, "elections/token_form.html", {"form": form, "election": election})

def ballot_view(request, election_id):
    election = get_object_or_404(Election, id=election_id)
    if not election.is_open():
        messages.error(request, "Election is not open at this time.")
        return redirect("elections:detail", election_id=election.id)

    voter_id = request.session.get("voter_id")
    if not voter_id:
        messages.error(request, "Please provide your token to proceed.")
        return redirect("elections:start_vote", election_id=election.id)

    voter = get_object_or_404(Voter, id=voter_id, election=election)
    # For simplicity: handle single ballot with one question. Extend for multiple ballots.
    ballot = election.ballots.order_by("order").first()
    if not ballot:
        messages.error(request, "No ballot is configured for this election.")
        return redirect("elections:detail", election_id=election.id)

    if election.ballot_type == "single":
        form_class = SingleChoiceForm
    elif election.ballot_type == "multiple":
        form_class = MultipleChoiceForm
    else:
        # future: ranked/score
        messages.error(request, "Ballot type not yet implemented in this demo.")
        return redirect("elections:detail", election_id=election.id)

    if request.method == "POST":
        form = form_class(ballot, request.POST)
        if form.is_valid():
            # Build payload
            if election.ballot_type == "single":
                choice_id = form.cleaned_data["choice"]
                payload = {"ballot_id": str(ballot.id), "choices": [choice_id]}
            else:
                choices = form.cleaned_data["choices"]
                payload = {"ballot_id": str(ballot.id), "choices": choices}

            # Save vote with transaction and prevent double-vote (atomic)
            with transaction.atomic():
                # reload voter row with lock to avoid race
                v = Voter.objects.select_for_update().get(pk=voter.pk)
                if v.has_voted:
                    messages.error(request, "Your token has already been used.")
                    return redirect("elections:detail", election_id=election.id)

                # create vote record
                ip = request_ip(request)
                vote = VoteRecord.objects.create(
                    election=election,
                    voter=v,
                    payload=payload,
                    ip_address=ip
                )
                # mark voter as having voted
                v.has_voted = True
                v.save(update_fields=["has_voted"])

                # audit log
                AuditLog.objects.create(
                    user=None,
                    action="cast_vote",
                    details={"voter_id": str(v.id), "vote_id": str(vote.id), "election": str(election.id)}
                )

            # clear session token
            request.session.pop("voter_id", None)
            # show confirmation
            return redirect("elections:vote_confirm", vote_id=vote.id)
    else:
        form = form_class(ballot)

    return render(request, "elections/ballot.html", {"form": form, "ballot": ballot, "election": election})

def vote_confirmation(request, vote_id):
    vote = get_object_or_404(VoteRecord, id=vote_id)
    # show msr (no revealing of vote mapping if you want anonymity — adjust accordingly)
    return render(request, "elections/vote_confirmation.html", {"vote": vote})
