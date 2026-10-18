"""Staff authentication views (voter auth lives in apps.voters)."""
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from apps.core.rate_limit import client_ip, rate_limit

from .forms import PasswordChangeForm, StyledAuthenticationForm


def _ident(request):
    return f"{client_ip(request)}:{request.POST.get('username', '')[:64]}"


@never_cache
@csrf_protect
@require_http_methods(["GET", "POST"])
@rate_limit("login", _ident)
def login_view(request):
    if request.user.is_authenticated:
        return redirect("core:dashboard-redirect")
    form = StyledAuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        login(request, user)
        # Session fixation defence: rotate session id on privilege change.
        request.session.cycle_key()
        return redirect("core:dashboard-redirect")
    return render(request, "accounts/login.html", {"form": form})


@require_http_methods(["POST"])
def logout_view(request):
    logout(request)
    return redirect("accounts:login")


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def password_change(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        request.session.cycle_key()
        messages.success(request, "Password updated.")
        return redirect("core:dashboard-redirect")
    return render(request, "accounts/password_change.html", {"form": form})
