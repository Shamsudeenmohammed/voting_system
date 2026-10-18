"""Audit browsing (auditors/admins) and checkpoint export."""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.core.permissions import user_orgs
from .models import AuditCheckpoint, AuditEvent
from .services import create_checkpoint, verify_chain, verify_checkpoint


@login_required
@never_cache
def event_list(request):
    org_ids = [m.organization_id for m in user_orgs(request.user)]
    if request.user.is_superuser:
        org_ids = None
    qs = AuditEvent.objects.select_related("actor", "organization")
    if org_ids is not None:
        if not org_ids:
            raise PermissionDenied
        qs = qs.filter(organization_id__in=org_ids)

    action = request.GET.get("action", "").strip()
    if action:
        qs = qs.filter(action__startswith=action)
    events = list(qs.order_by("-seq")[:200])
    actions = sorted(
        AuditEvent.objects.values_list("action", flat=True).distinct()[:200]
    )
    return render(
        request,
        "audit/events.html",
        {"events": events, "actions": actions, "selected_action": action},
    )


@login_required
@require_http_methods(["POST"])
def run_verify(request):
    org_ids = [m.organization_id for m in user_orgs(request.user)]
    if not org_ids and request.user.is_superuser:
        org_ids = [None]
    if not org_ids:
        raise PermissionDenied
    results = []
    for org_id in org_ids:
        from apps.orgs.models import Organization

        org = Organization.objects.filter(pk=org_id).first() if org_id else None
        results.append({"org": org, "result": verify_chain(organization=org)})
    ok = all(r["result"]["ok"] for r in results)
    if ok:
        messages.success(request, "Audit chain verified — no breaks detected.")
    else:
        messages.error(request, "AUDIT CHAIN BREAK DETECTED — see details.")
    return render(request, "audit/verify.html", {"results": results})


@login_required
@never_cache
def verify_page(request):
    return render(request, "audit/verify.html", {"results": None})


@login_required
@require_http_methods(["POST"])
def checkpoint(request):
    org_ids = [m.organization_id for m in user_orgs(request.user)]
    if request.user.is_superuser and not org_ids:
        org_ids = [None]
    if not org_ids:
        raise PermissionDenied
    from apps.orgs.models import Organization

    created = []
    for org_id in org_ids:
        org = Organization.objects.filter(pk=org_id).first() if org_id else None
        try:
            cp = create_checkpoint(organization=org, user=request.user)
            created.append(cp)
        except ValueError:
            continue
    if not created:
        messages.error(request, "No audit events to checkpoint.")
        return redirect("audit:events")
    # Provide downloadable checkpoint file (operator must store off-host).
    payload = json.dumps(
        [
            {
                "id": cp.pk,
                "org": cp.organization_id,
                "last_seq": cp.last_seq,
                "head_hash": cp.head_hash,
                "signature": cp.signature,
                "created_at": cp.created_at.isoformat(),
            }
            for cp in created
        ],
        indent=2,
    )
    resp = HttpResponse(payload, content_type="application/json")
    resp["Content-Disposition"] = 'attachment; filename="audit_checkpoints.json"'
    messages.success(
        request,
        "Checkpoint created. COPY THIS FILE TO EXTERNAL STORAGE — it is your "
        "independent anchor of the audit chain.",
    )
    return resp


@login_required
def checkpoint_list(request):
    org_ids = [m.organization_id for m in user_orgs(request.user)]
    if request.user.is_superuser:
        qs = AuditCheckpoint.objects.select_related("organization", "created_by")
    else:
        if not org_ids:
            raise PermissionDenied
        qs = AuditCheckpoint.objects.filter(organization_id__in=org_ids).select_related(
            "organization", "created_by"
        )
    checkpoints = list(qs.order_by("-created_at")[:100])
    for cp in checkpoints:
        cp.valid = verify_checkpoint(cp)
    return render(
        request, "audit/checkpoints.html", {"checkpoints": checkpoints}
    )
