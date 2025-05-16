from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Sum, Q, Count, Prefetch, Max
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.views.decorators.cache import cache_page
from django.views.decorators.http import require_http_methods
from django.views.decorators.vary import vary_on_cookie
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect

from reservations.models import Reservation, Leg
from .emails import thankyou_email
from .forms import (
    CustomUserCreationForm,
    PartnerFormSubmission,
    ContactUsFormSubmission,
)
from .models import (
    NewsLetter,
    NewsletterSubscriptionAttempt,
    TravelAgent,
    CommissionPayout,
)
import logging

logger = logging.getLogger(__name__)


def thankYou(request):
    """Display thank you page."""
    return render(request, "users/thank-you.html")


def registerUser(request):
    """Handle user registration."""
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
    """Handle user login."""
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
                return redirect("schedule")
        else:
            messages.error(
                request, "Please Enter Valid Credentials", extra_tags="danger"
            )

    return render(request, "users/login_register.html", {"page": page})


@login_required(login_url="login")
def logoutUser(request):
    """Log out the user once they click logout."""
    logout(request)
    messages.success(request, "You Have Been Logged Out!")
    return redirect("login")


def partner(request):
    """Handle partner form submission."""
    if request.method == "POST":
        form = PartnerFormSubmission(request.POST)
        if form.is_valid():
            instance = form.save()
            return redirect("thankyou")
    else:
        form = PartnerFormSubmission()

    context = {"form": form}
    return render(request, "users/become_partner.html", context)


def contact(request):
    """Handle contact form submission."""
    if request.method == "POST":
        form = ContactUsFormSubmission(request.POST)
        if form.is_valid():
            instance = form.save()
            return redirect("thankyou")
    else:
        form = ContactUsFormSubmission()

    context = {"form": form}
    return render(request, "reservations/contact.html", context)


def newsletter_subscribe(request):
    """Handle newsletter subscription."""
    if request.method == "POST":
        email = request.POST.get("email")
        name = request.POST.get("name")
        if email:
            x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
            if x_forwarded_for:
                ip_address = x_forwarded_for.split(",")[0]
            else:
                ip_address = request.META.get("REMOTE_ADDR")

            # Record the attempt
            attempt = NewsletterSubscriptionAttempt.objects.create(
                ip_address=ip_address, email=email
            )

            # Check if email already exists
            if not NewsLetter.objects.filter(email=email).exists():
                NewsLetter.objects.create(email=email, name=name)
                attempt.success = True
                attempt.save()
                messages.success(
                    request,
                    "Thank you for subscribing to our newsletter!",
                    extra_tags="newsletter_success",
                )
            else:
                messages.info(
                    request,
                    "You are already subscribed to our newsletter!",
                    extra_tags="newsletter_info",
                )
        else:
            messages.error(
                request,
                "Please provide a valid email address.",
                extra_tags="newsletter_error",
            )

    # Redirect back to the previous page
    return redirect(request.META.get("HTTP_REFERER", "/"))


from django.db import transaction


def register_agent(request):
    from .emails import agent_register_email

    """Handle travel agent registration."""
    if request.method == "POST":
        # Get form data
        form_data = {
            "username": request.POST.get("username"),
            "email": request.POST.get("email"),
            "agent_name": request.POST.get("agent_name"),
            "agency_name": request.POST.get("agency_name"),
            "phone": request.POST.get("phone"),
            "payment_info": request.POST.get("payment_info"),
            "payment_method": request.POST.get("payment_method"),
        }

        password1 = request.POST.get("password1")
        password2 = request.POST.get("password2")

        # Validation context for errors
        error_context = {"form_data": form_data}

        # Validate passwords match
        if password1 != password2:
            messages.error(request, "Passwords do not match.")
            return render(request, "users/register_agent.html", error_context)

        # Check if username or email already exists
        if User.objects.filter(username=form_data["username"]).exists():
            messages.error(request, "Username already exists.")
            return render(request, "users/register_agent.html", error_context)

        if User.objects.filter(email=form_data["email"]).exists():
            messages.error(request, "Email already exists.")
            return render(request, "users/register_agent.html", error_context)

        try:
            # Use transaction to ensure both user and agent are created together
            with transaction.atomic():
                # Create user account
                user = User.objects.create_user(
                    username=form_data["username"],
                    email=form_data["email"],
                    password=password1,
                )

                # Create travel agent profile
                travel_agent = TravelAgent.objects.create(
                    user=user,
                    agent_name=form_data["agent_name"],
                    agency_name=form_data["agency_name"],
                    phone=form_data["phone"],
                    payment_method=form_data["payment_method"],
                    payment_info=form_data["payment_info"],
                )

            login(request, user)
            messages.success(request, "Successfully registered as a travel agent!")
            return redirect("agent_dashboard")

        except Exception as e:
            # The transaction will automatically rollback, deleting the user if agent creation failed
            messages.error(request, f"Error creating account: {str(e)}")
            return render(request, "users/register_agent.html", error_context)

    return render(request, "users/register_agent.html")


def rate_limit(key_prefix, limit=60, period=60):
    """Rate limiting decorator."""

    def decorator(view_func):
        def wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return view_func(request, *args, **kwargs)

            cache_key = f"{key_prefix}_{request.user.id}"
            count = cache.get(cache_key, 0)

            if count >= limit:
                messages.error(request, "Too many requests. Please try again later.")
                return redirect("agent_dashboard")

            cache.set(cache_key, count + 1, period)
            return view_func(request, *args, **kwargs)

        return wrapped_view

    return decorator


def agent_required(view_func):
    """Decorator to ensure only agents can access certain views."""

    @login_required
    def wrapper(request, *args, **kwargs):
        if not is_agent(request.user):
            messages.error(
                request, "You must be a registered travel agent to access this page."
            )
            return redirect("agent_login")
        return view_func(request, *args, **kwargs)

    return wrapper


def agent_login(request):
    """Dedicated login view for travel agents."""
    if request.user.is_authenticated:
        try:
            TravelAgent.objects.get(user=request.user)
            return redirect("agent_dashboard")
        except TravelAgent.DoesNotExist:
            messages.error(request, "You are not registered as a travel agent.")
            logout(request)

    if request.method == "POST":
        username = request.POST["username"]
        password = request.POST["password"]
        user = authenticate(request, username=username, password=password)

        if user is not None:
            try:
                TravelAgent.objects.get(user=user)
                login(request, user)
                messages.success(request, "Successfully logged in as travel agent")
                return redirect("agent_dashboard")
            except TravelAgent.DoesNotExist:
                messages.error(
                    request, "This account is not registered as a travel agent."
                )
        else:
            messages.error(request, "Invalid credentials")

    return render(request, "users/agent_login.html")


@agent_required
def agent_dashboard(request):
    """Display travel agent dashboard."""
    try:
        # Get travel agent with user data in a single query and cache it
        travel_agent = TravelAgent.objects.select_related("user").get(user=request.user)

        # Get filters from query params
        status = request.GET.get("status", "all")
        date_filter = request.GET.get("date_filter", "all")
        search_query = request.GET.get("search", "")

        # Build base queryset with all necessary related data in a single query
        base_queryset = (
            Reservation.objects.filter(travel_agent=travel_agent)
            .select_related(
                "customer", "vehicle", "rate", "travel_agent", "travel_agent__user"
            )
            .prefetch_related(
                Prefetch(
                    "legs",
                    queryset=Leg.objects.select_related("flight_information").order_by(
                        "pickup_date", "pickup_time"
                    ),
                )
            )
        )

        # Apply search filter if provided
        if search_query:
            base_queryset = base_queryset.filter(
                Q(customer__first_name__icontains=search_query)
                | Q(customer__last_name__icontains=search_query)
                | Q(customer__email__icontains=search_query)
                | Q(uuid__icontains=search_query)
            )

        # Get counts and stats in a single aggregation query
        stats = base_queryset.aggregate(
            total_count=Count("id"),
            pending_count=Count("id", filter=Q(status="pending")),
            confirmed_count=Count("id", filter=Q(status="confirmed")),
            completed_count=Count("id", filter=Q(status="completed")),
            cancelled_count=Count("id", filter=Q(status="cancelled")),
        )

        # Apply status filter
        if status != "all":
            base_queryset = base_queryset.filter(status=status)

        # Apply date filter with timezone-aware datetime
        now = timezone.now()
        if date_filter == "today":
            base_queryset = base_queryset.filter(created_at__date=now.date())
        elif date_filter == "week":
            week_ago = now - timedelta(days=7)
            base_queryset = base_queryset.filter(created_at__gte=week_ago)
        elif date_filter == "month":
            month_ago = now - timedelta(days=30)
            base_queryset = base_queryset.filter(created_at__gte=month_ago)
        elif date_filter == "year":
            year_ago = now - timedelta(days=365)
            base_queryset = base_queryset.filter(created_at__gte=year_ago)

        # Get ordered reservations for pagination
        ordered_reservations = base_queryset.order_by("-created_at")

        # Get recent activity from the same queryset to avoid duplicate queries
        recent_activity = list(ordered_reservations[:5])

        # Add last leg to each reservation in recent activity
        for reservation in recent_activity:
            legs = list(reservation.legs.all())
            if legs:
                reservation.last_leg = max(
                    legs, key=lambda leg: (leg.pickup_date, leg.pickup_time)
                )
            else:
                reservation.last_leg = None

        # Paginate reservations
        paginator = Paginator(ordered_reservations, 10)
        page_number = request.GET.get("page")
        page_obj = paginator.get_page(page_number)

        # Calculate total commission including all types
        total_commission = (
            travel_agent.total_paid_commission
            + travel_agent.unpaid_commissions
            + travel_agent.pending_commissions
        )

        context = {
            "travel_agent": travel_agent,
            "reservations": page_obj,
            "total_commission": total_commission,
            "paid_commission": travel_agent.total_paid_commission,
            "unpaid_commission": travel_agent.unpaid_commissions,
            "pending_commission": travel_agent.pending_commissions,
            "pending_count": stats["pending_count"],
            "confirmed_count": stats["confirmed_count"],
            "completed_count": stats["completed_count"],
            "cancelled_count": stats["cancelled_count"],
            "status": status,
            "date_filter": date_filter,
            "search_query": search_query,
            "recent_activity": recent_activity,
        }
        return render(request, "users/agent_dashboard.html", context)

    except TravelAgent.DoesNotExist:
        messages.error(request, "You are not registered as a travel agent.")
        return redirect("register_agent")


@agent_required
def agent_commission_history(request):
    """Display travel agent commission history."""
    try:
        travel_agent = TravelAgent.objects.select_related("user").get(user=request.user)

        # Get paid commissions with all reservations prefetched
        payouts = (
            CommissionPayout.objects.filter(agent=travel_agent)
            .prefetch_related(
                Prefetch(
                    "reservations",
                    queryset=Reservation.objects.select_related("customer", "vehicle"),
                )
            )
            .order_by("-paid_at")
        )

        # Get unpaid commissions - optimized
        unpaid_reservations = (
            Reservation.objects.filter(
                travel_agent=travel_agent, commission_paid=False, status="completed"
            )
            .select_related("customer", "vehicle")
            .order_by("-created_at")
        )

        # Ensure agent's unpaid_commissions is up-to-date
        travel_agent.update_unpaid_commissions()

        # Paginate payouts
        paginator = Paginator(payouts, 10)
        page_number = request.GET.get("page")
        page_obj = paginator.get_page(page_number)

        context = {
            "travel_agent": travel_agent,
            "payouts": page_obj,
            "unpaid_reservations": unpaid_reservations,
            "total_paid": travel_agent.total_paid_commission,
            "total_unpaid": travel_agent.unpaid_commissions,
        }
        return render(request, "users/agent_commission_history.html", context)

    except TravelAgent.DoesNotExist:
        messages.error(request, "You are not registered as a travel agent.")
        return redirect("register_agent")


@agent_required
def agent_reservation_detail(request, uuid):
    """Display travel agent reservation detail."""
    try:
        travel_agent = TravelAgent.objects.select_related("user").get(user=request.user)

        # Get reservation with all related data in one query
        reservation = get_object_or_404(
            Reservation.objects.select_related(
                "customer", "vehicle", "rate", "travel_agent__user"
            ).prefetch_related(
                Prefetch(
                    "legs",
                    queryset=Leg.objects.select_related("flight_information").order_by(
                        "pickup_date", "pickup_time"
                    ),
                )
            ),
            uuid=uuid,
            travel_agent=travel_agent,
        )

        context = {
            "travel_agent": travel_agent,
            "reservation": reservation,
        }
        return render(request, "users/agent_reservation_detail.html", context)

    except TravelAgent.DoesNotExist:
        messages.error(request, "You are not registered as a travel agent.")
        return redirect("register_agent")


@agent_required
def agent_profile(request):
    """Handle travel agent profile management."""
    try:
        travel_agent = TravelAgent.objects.get(user=request.user)

        if request.method == "POST":
            # Update profile information
            travel_agent.agent_name = request.POST.get("agent_name", "")
            travel_agent.agency_name = request.POST.get("agency_name", "")
            travel_agent.phone = request.POST.get("phone", "")
            travel_agent.payment_info = request.POST.get("payment_info", "")
            travel_agent.save()
            messages.success(request, "Profile updated successfully!")
            return redirect("agent_profile")

        context = {
            "travel_agent": travel_agent,
        }
        return render(request, "users/agent_profile.html", context)

    except TravelAgent.DoesNotExist:
        messages.error(request, "You are not registered as a travel agent.")
        return redirect("register_agent")


def is_agent(user):
    """Check if user is a travel agent."""
    if not user.is_authenticated:
        return False
    return TravelAgent.objects.filter(user=user).exists()
