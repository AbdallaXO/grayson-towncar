"""Adding a driver and getting them signed in — without anyone typing a
password for them.

    Staff:   /drivers/new/                   create the account, send the link
             /drivers/<id>/invite/  (POST)   resend / copy / revoke from the profile
    Driver:  /drivers/welcome/<token>/       set username + password, no login needed
             /drivers/my-details/            confirm phone, email, address

The rule for who may add a driver is the same one the profile page uses for
editing: superusers only. Dispatcher logins (is_staff) can see everything and
change nothing here.
"""
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from drivers import invites, paperwork
from drivers.forms import DriverMyDetailsForm, DriverWelcomeForm, NewDriverForm
from drivers.models import Driver
from drivers.paperwork import first_name_for
from users.models import UserProfile

SENT_MESSAGES = {
    "sms": "Welcome link texted to {to}. It works for 7 days.",
    "email": "Welcome link emailed to {to}. It works for 7 days.",
    "link": "Welcome link ready — copy it from the Account card below and send it however you like.",
}


def _may_manage(user):
    return user.is_authenticated and user.is_superuser


# ── Staff ────────────────────────────────────────────────────────────────────

@login_required(login_url="login")
def driver_new(request):
    if not request.user.is_staff:
        return redirect("home")
    if not _may_manage(request.user):
        return HttpResponseForbidden("Only an administrator can add drivers.")

    form = NewDriverForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        with transaction.atomic():
            username = invites.suggest_username(d["first_name"], d["last_name"], d["phone_number"])
            # No password: create_user() marks it unusable until the driver sets
            # one through the welcome link.
            user = User.objects.create_user(
                username=username,
                email=d["email"],
                first_name=d["first_name"].strip(),
                last_name=(d["last_name"] or "").strip(),
            )
            UserProfile.objects.get_or_create(
                user=user, defaults={"phone_number": d["phone_number"], "is_driver": True},
            )
            driver = Driver.objects.create(
                profile=user,
                phone_number=d["phone_number"],
                driver_type=d["driver_type"],
                portal_role=d["portal_role"],
                employment_type=d["employment_type"] or "",
                hired_on=d["hired_on"],
            )
            invite = invites.create(driver, by=request.user)

        ok, err = invites.deliver(invite, d["send_via"])
        messages.success(request, f"{driver} added to the directory.")
        if ok:
            messages.success(request, SENT_MESSAGES[d["send_via"]].format(to=invite.sent_to))
        else:
            messages.error(request, err)
        return redirect("driver_profile", driver_id=driver.id)

    return render(request, "drivers/driver_new.html", {"form": form})


@login_required(login_url="login")
@require_POST
def driver_invite(request, driver_id):
    if not _may_manage(request.user):
        return HttpResponseForbidden("Only an administrator can send welcome links.")
    driver = get_object_or_404(Driver.objects.select_related("profile"), id=driver_id)
    action = request.POST.get("action", "link")

    if action == "revoke":
        invites.revoke_open(driver)
        messages.success(request, "The welcome link no longer works.")
        return redirect("driver_profile", driver_id=driver.id)

    if action == "new":
        invite = invites.create(driver, by=request.user)
        via = request.POST.get("via", "link")
    else:
        via = action if action in ("sms", "email", "link") else "link"
        invite = invites.current_or_new(driver, by=request.user)

    ok, err = invites.deliver(invite, via)
    if ok:
        messages.success(request, SENT_MESSAGES[via].format(to=invite.sent_to))
    else:
        messages.error(request, err)
    return redirect("driver_profile", driver_id=driver.id)


# ── Driver ───────────────────────────────────────────────────────────────────

@never_cache
def driver_welcome(request, token):
    """Public: the page the welcome link opens. No login — the token is the login."""
    invite = invites.lookup(token)
    if invite is None:
        spent = invites.lookup_any(token)
        return render(
            request,
            "drivers/welcome_invalid.html",
            {"reason": spent.status if spent else "unknown", "company_phone": invites.COMPANY_PHONE},
            status=410 if spent else 404,
        )

    driver = invite.driver
    user = driver.profile
    if request.method == "POST":
        form = DriverWelcomeForm(user, request.POST, request.FILES)
        if form.is_valid():
            user = invites.accept(invite, form.cleaned_data["new_password1"], form.cleaned_data["username"])
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            request.session["login_type"] = "main"
            if driver.is_operator:
                messages.success(request, "You're all set. This is your job board.")
                return redirect("operator_board")
            details_url = f"{reverse('driver_my_details')}?welcome=1"
            upload = form.cleaned_data.get("license_scan")
            if upload:
                # They added their license on the welcome page: read it and let
                # them confirm what it says, then carry on to My Details.
                from drivers.views import license_scan_confirm_response
                messages.success(request, "Your login is set. Now check what we read off your license.")
                return license_scan_confirm_response(request, driver, upload, next_url=details_url)
            messages.success(
                request,
                "Your login is set. One more minute: check your details below so the office has them.",
            )
            return redirect(details_url)
    else:
        form = DriverWelcomeForm(user)

    return render(request, "drivers/welcome.html", {
        "form": form,
        "driver": driver,
        "first_name": first_name_for(driver),
        "returning": driver.has_login(),
        "company_phone": invites.COMPANY_PHONE,
        "expires_at": invite.expires_at,
    })


@login_required(login_url="login")
def my_details(request):
    """The chauffeur's own contact details, editable in the driver app."""
    driver = get_object_or_404(Driver.objects.select_related("profile"), profile=request.user)
    welcome = request.GET.get("welcome") == "1"
    # A license photo is part of the form until one is on file. Once it is,
    # the field disappears and replacing it lives on My Documents.
    needs_license = not (driver.license_scan or driver.license_number)

    if request.method == "POST":
        form = DriverMyDetailsForm(request.POST, request.FILES, instance=driver, require_license=needs_license)
        if form.is_valid():
            form.save()
            upload = form.cleaned_data.get("license_scan")
            if upload:
                # Same read-the-card-and-confirm step as My Documents. It
                # renders the confirm page itself, so the driver goes straight
                # from "details saved" to "check what we read off your license".
                from drivers.views import license_scan_confirm_response
                messages.success(request, "Your details are saved.")
                return license_scan_confirm_response(request, driver, upload)
            if welcome:
                messages.success(
                    request,
                    "Thanks! Last step: take a photo of your chauffeur permit so the office has it on file.",
                )
                return redirect("driver_my_documents")
            messages.success(request, "Your details are saved. Thank you!")
            return redirect("driver_my_details")
        messages.error(request, "Please check the highlighted fields.")
    else:
        form = DriverMyDetailsForm(instance=driver, require_license=needs_license)

    summary = paperwork.summarize(driver, with_urls=False)
    return render(request, "drivers/my_details.html", {
        "driver": driver,
        "form": form,
        "welcome": welcome,
        "needs_license": needs_license,
        "docs": [d for d in summary["docs"] if d["key"] != "dot" or d["state"] != paperwork.MISSING],
        "docs_owed": summary["owed"],
    })


@login_required(login_url="login")
def my_password(request):
    """Change password from inside the driver app (the signed-in counterpart to
    the welcome link and the emailed reset)."""
    driver = get_object_or_404(Driver.objects.select_related("profile"), profile=request.user)
    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)  # keep this session signed in
            messages.success(request, "Your password is changed. Use the new one next time you sign in.")
            return redirect("driver_my_details")
        messages.error(request, "Please check the highlighted fields.")
    else:
        form = PasswordChangeForm(request.user)
    for f in form.fields.values():
        f.widget.attrs.setdefault("class", "gt-input")
        f.widget.attrs.setdefault("autocomplete", "new-password" if "new" in f.label.lower() else "current-password")
        f.help_text = ""
    return render(request, "drivers/my_password.html", {"driver": driver, "form": form})
