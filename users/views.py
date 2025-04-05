from django.shortcuts import render
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from .forms import CustomUserCreationForm, PartnerFormSubmission


def index(request):
    return render(request, "users/profiles.html")


def partner(request):
    return render(request, "users/become_partner.html")


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
    if request.user.is_authenticated:
        return redirect("home")
    if request.method == "POST":
        username = request.POST["username"]
        password = request.POST["password"]
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            messages.success(request, "Successfully logged in", extra_tags="success")
            return redirect("home")
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
        # create dict that renders data from html
        form_data = {
            "name": request.POST.get("name"),
            "email": request.POST.get("email"),
            "phone_number": request.POST.get("phone"),
            "preferred_contact": request.POST.get("contactMethod"),
            "agency_name": request.POST.get("agencyName"),
            "agency_website": request.POST.get("agencyWebsite") or None,
            "referral_source": request.POST.get("referralSource"),
            "additional_info": request.POST.get("additionalInfo") or None,
        }
        form = PartnerFormSubmission(form_data)
        if form.is_valid():
            form.save()
            messages.success(
                request, "Your partnership request has been submitted successfully!"
            )
            return redirect("partner")
        else:
            messages.error(request, "Please correct errors below.")
    else:
        form = PartnerFormSubmission()
    return render(request, "users/become_partner.html", {"form": form})
