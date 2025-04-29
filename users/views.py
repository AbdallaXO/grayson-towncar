from django.shortcuts import render
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from .forms import (
    CustomUserCreationForm,
    PartnerFormSubmission,
    ContactUsFormSubmission,
)
from .emails import thankyou_email
from django.db import transaction


def thankYou(request):
    return render(request, "users/thank-you.html")


def registerUser(request):
    form = CustomUserCreationForm()
    page = "register"
    context = {"page": page, "form": form}
    if request.method == "POST":
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.username = user.username.lower()
            user.save()
            messages.success(
                request,
                f"Hello {user.username} Your account was created successfully!",
                extra_tags="success",
            )

            login(request, user)
            return redirect("home")
        else:
            messages.error(
                request,
                "An Error has Occoured during registration.",
                extra_tags="danger",
            )
    return render(request, "users/login_register.html", context)


def loginUser(request):
    page = "login"
    if request.method == "POST":
        username = request.POST["username"]
        password = request.POST["password"]
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            messages.success(request, "Successfully logged in", extra_tags="success")
            if request.user.is_superuser:
                return redirect("dashboard")
            else:
                return redirect("drivers_dashboard")
        else:
            messages.error(
                request, "Please Enter Valid Credentials", extra_tags="danger"
            )

    return render(request, "users/login_register.html", {"page": page})


@login_required(login_url="login")
def logoutUser(request):
    """
    Simply Logs out the user once they click logout
    """
    logout(request)
    messages.success(request, "You Have Been Logged Out!")
    return redirect("login")


def partner(request):
    if request.method == "POST":
        form = PartnerFormSubmission(request.POST)
        if form.is_valid():
            instance = form.save()
            transaction.on_commit(lambda: thankyou_email(instance))
            return redirect("thankyou")

    else:
        form = PartnerFormSubmission()
    context = {"form": form}
    return render(request, "users/become_partner.html", context)


def contact(request):
    if request.method == "POST":
        form = ContactUsFormSubmission(request.POST)
        if form.is_valid():
            instance = form.save()
            transaction.on_commit(lambda: thankyou_email(instance))
            return redirect("thankyou")
    else:
        form = ContactUsFormSubmission()
    context = {"form": form}

    return render(request, "reservations/contact.html", context)
