"""
User and Agency Management Views

This module contains all views related to user authentication, travel agent management,
and agency operations including commission tracking and payouts.
"""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Sum, Q, Count, Prefetch
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.generic import DetailView, ListView, UpdateView, TemplateView
from django.urls import reverse_lazy
from django.http import Http404, JsonResponse
import logging

from reservations.models import Reservation, Leg
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
    Agency,
    AgencyCommissionPayout,
)

logger = logging.getLogger(__name__)


def format_decimal(value):
    """Helper function to format decimal values to 2 decimal places."""
    if value is None:
        return 0.00
    return float(round(value, 2))


# =============================================
# PUBLIC & UTILITY VIEWS
# =============================================


def thankYou(request):
    """Display thank you page for form submissions."""
    return render(request, "users/thank-you.html")


def partner(request):
    """Handle partner form submission."""
    if request.method == "POST":
        form = PartnerFormSubmission(request.POST)
        if form.is_valid():
            form.save()
            return redirect("thankyou")
    else:
        form = PartnerFormSubmission()

    return render(request, "users/become_partner.html", {"form": form})


def contact(request):
    """Handle contact form submission."""
    if request.method == "POST":
        form = ContactUsFormSubmission(request.POST)
        if form.is_valid():
            form.save()
            return redirect("thankyou")
    else:
        form = ContactUsFormSubmission()

    return render(request, "reservations/contact.html", {"form": form})


def newsletter_subscribe(request):
    """Handle newsletter subscription with IP tracking and duplicate prevention."""
    if request.method == "POST":
        email = request.POST.get("email")
        name = request.POST.get("name")

        if email:
            # Get client IP address
            x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
            ip_address = (
                x_forwarded_for.split(",")[0]
                if x_forwarded_for
                else request.META.get("REMOTE_ADDR")
            )

            # Record subscription attempt
            attempt = NewsletterSubscriptionAttempt.objects.create(
                ip_address=ip_address, email=email
            )

            # Check for existing subscription
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

    return redirect(request.META.get("HTTP_REFERER", "/"))


# =============================================
# AUTHENTICATION VIEWS
# =============================================


def registerUser(request):
    """Handle user registration for regular users."""
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
                "An Error has Occurred during registration.",
                extra_tags="danger",
            )
    else:
        form = CustomUserCreationForm()

    return render(
        request, "users/login_register.html", {"page": "register", "form": form}
    )


def loginUser(request):
    """Handle user login for admins and drivers only."""
    if request.method == "POST":
        username = request.POST["username"]
        password = request.POST["password"]

        # Try to find user with case-insensitive lookup
        try:
            user_obj = User.objects.get(username__iexact=username)
            # Use the actual username from database for authentication
            user = authenticate(request, username=user_obj.username, password=password)
        except User.DoesNotExist:
            user = None

        if user is not None:
            # Check if this user is a travel agent
            try:
                TravelAgent.objects.get(user=user)
                messages.info(
                    request, "Travel agents should use the dedicated agent login."
                )
                return redirect("agent_login")
            except TravelAgent.DoesNotExist:
                pass  # Not an agent, continue with normal flow

            login(request, user)
            request.session["login_type"] = "main"
            messages.success(request, "Successfully logged in", extra_tags="success")

            # Role-based redirection for non-agents
            return redirect("dashboard" if user.is_superuser else "schedule")
        else:
            messages.error(
                request, "Please Enter Valid Credentials", extra_tags="danger"
            )

    return render(request, "users/login_register.html", {"page": "login"})


@login_required(login_url="login")
def logoutUser(request):
    """Log out user and redirect to appropriate login page."""
    login_type = request.session.get("login_type", "main")

    logout(request)
    messages.success(request, "You Have Been Logged Out!")

    # Redirect based on how they logged in
    if login_type == "agent":
        return redirect("agent_login")
    else:
        return redirect("login")


# =============================================
# TRAVEL AGENT VIEWS & DECORATORS
# =============================================


def is_agent(user):
    """Check if user is a registered travel agent."""
    if not user.is_authenticated:
        return False
    return TravelAgent.objects.filter(user=user).exists()


def agent_required(view_func):
    """Decorator to ensure only registered agents can access certain views."""

    @login_required
    def wrapper(request, *args, **kwargs):
        if not is_agent(request.user):
            messages.error(
                request, "You must be a registered travel agent to access this page."
            )
            return redirect("agent_login")
        return view_func(request, *args, **kwargs)

    return wrapper


def rate_limit(key_prefix, limit=60, period=60):
    """Rate limiting decorator to prevent abuse."""

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


def register_agent(request):
    """Handle travel agent registration with user account creation."""
    if request.method == "POST":
        # Extract form data
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
        error_context = {"form_data": form_data}

        # Validate passwords
        if password1 != password2:
            messages.error(request, "Passwords do not match.")
            return render(request, "users/register_agent.html", error_context)

        # Check for existing accounts
        if User.objects.filter(username=form_data["username"]).exists():
            messages.error(request, "Username already exists.")
            return render(request, "users/register_agent.html", error_context)

        if User.objects.filter(email=form_data["email"]).exists():
            messages.error(request, "Email already exists.")
            return render(request, "users/register_agent.html", error_context)

        try:
            # Create user and agent profile atomically
            with transaction.atomic():
                user = User.objects.create_user(
                    username=form_data["username"],
                    email=form_data["email"],
                    password=password1,
                )

                TravelAgent.objects.create(
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
            messages.error(request, f"Error creating account: {str(e)}")
            return render(request, "users/register_agent.html", error_context)

    return render(request, "users/register_agent.html")


def agent_login(request):
    """Dedicated login view for travel agents with validation."""
    # Redirect if already authenticated agent
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

        # Try to find user with case-insensitive lookup
        try:
            user_obj = User.objects.get(username__iexact=username)
            # Use the actual username from database for authentication
            user = authenticate(request, username=user_obj.username, password=password)
        except User.DoesNotExist:
            user = None

        if user is not None:
            try:
                TravelAgent.objects.get(user=user)
                login(request, user)
                request.session["login_type"] = "agent"  # Add this line
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
    """
    Main dashboard for travel agents with filtering, search, and pagination.
    Displays reservations, commission stats, and recent activity.
    """
    try:
        travel_agent = TravelAgent.objects.select_related("user").get(user=request.user)

        # Get query parameters for filtering
        status = request.GET.get("status", "all")
        date_filter = request.GET.get("date_filter", "all")
        search_query = request.GET.get("search", "")

        # Build optimized base queryset
        base_queryset = (
            Reservation.objects.filter(travel_agent=travel_agent)
            .select_related("customer", "vehicle", "rate", "travel_agent__user")
            .prefetch_related(
                Prefetch(
                    "legs",
                    queryset=Leg.objects.select_related("flight_information").order_by(
                        "pickup_date", "pickup_time"
                    ),
                )
            )
        )

        # Apply search filter
        if search_query:
            base_queryset = base_queryset.filter(
                Q(customer__first_name__icontains=search_query)
                | Q(customer__last_name__icontains=search_query)
                | Q(customer__email__icontains=search_query)
                | Q(uuid__icontains=search_query)
            )

        # Calculate stats with single query
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

        # Apply date filters
        now = timezone.now()
        date_filters = {
            "today": now.date(),
            "week": now - timedelta(days=7),
            "month": now - timedelta(days=30),
            "year": now - timedelta(days=365),
        }

        if date_filter == "today":
            base_queryset = base_queryset.filter(
                created_at__date=date_filters[date_filter]
            )
        elif date_filter in ["week", "month", "year"]:
            base_queryset = base_queryset.filter(
                created_at__gte=date_filters[date_filter]
            )

        # Order and paginate
        ordered_reservations = base_queryset.order_by("-created_at")

        # Get recent activity
        recent_activity = list(ordered_reservations[:5])
        for reservation in recent_activity:
            legs = list(reservation.legs.all())
            if legs:
                reservation.last_leg = max(
                    legs, key=lambda leg: (leg.pickup_date, leg.pickup_time)
                )

        # Paginate results
        paginator = Paginator(ordered_reservations, 10)
        page_number = request.GET.get("page")
        page_obj = paginator.get_page(page_number)

        context = {
            "travel_agent": travel_agent,
            "reservations": page_obj,
            "total_commission": format_decimal(
                travel_agent.total_paid_commission
                + travel_agent.unpaid_commissions
                + travel_agent.pending_commissions
            ),
            "paid_commission": format_decimal(travel_agent.total_paid_commission),
            "unpaid_commission": format_decimal(travel_agent.unpaid_commissions),
            "pending_commission": format_decimal(travel_agent.pending_commissions),
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
    """Display travel agent commission history with payouts and unpaid commissions."""
    try:
        travel_agent = TravelAgent.objects.select_related("user").get(user=request.user)

        # Get commission payouts with optimized prefetch
        payouts = (
            CommissionPayout.objects.filter(agent=travel_agent)
            .select_related("agent", "agent__user")
            .prefetch_related(
                Prefetch(
                    "reservations",
                    queryset=Reservation.objects.select_related(
                        "customer",
                        "vehicle",
                        "rate",
                        "rate__route",
                        "rate__route__origin",
                        "rate__route__destination",
                    ),
                )
            )
            .order_by("-paid_at")
        )

        # Get unpaid reservations with optimized prefetch
        unpaid_reservations = (
            Reservation.objects.filter(
                travel_agent=travel_agent, commission_paid=False, status="completed"
            )
            .select_related(
                "customer",
                "vehicle",
                "rate",
                "rate__route",
                "rate__route__origin",
                "rate__route__destination",
            )
            .order_by("-created_at")
        )

        # Update agent's unpaid commissions
        travel_agent.update_unpaid_commissions()

        # Paginate payouts
        paginator = Paginator(payouts, 10)
        page_number = request.GET.get("page")
        page_obj = paginator.get_page(page_number)

        context = {
            "travel_agent": travel_agent,
            "payouts": page_obj,
            "unpaid_reservations": unpaid_reservations,
            "total_paid": format_decimal(travel_agent.total_paid_commission),
            "total_unpaid": format_decimal(travel_agent.unpaid_commissions),
        }
        return render(request, "users/agent_commission_history.html", context)

    except TravelAgent.DoesNotExist:
        messages.error(request, "You are not registered as a travel agent.")
        return redirect("register_agent")


@agent_required
def agent_reservation_detail(request, uuid):
    """Display detailed reservation information for travel agents."""
    try:
        travel_agent = TravelAgent.objects.select_related("user").get(user=request.user)

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
    """Handle travel agent profile viewing and updates."""
    try:
        travel_agent = TravelAgent.objects.get(user=request.user)

        if request.method == "POST":
            # Update profile fields
            travel_agent.agent_name = request.POST.get("agent_name", "")
            travel_agent.agency_name = request.POST.get("agency_name", "")
            travel_agent.phone = request.POST.get("phone", "")
            travel_agent.payment_info = request.POST.get("payment_info", "")
            travel_agent.save()

            messages.success(request, "Profile updated successfully!")
            return redirect("agent_profile")

        context = {"travel_agent": travel_agent}
        return render(request, "users/agent_profile.html", context)

    except TravelAgent.DoesNotExist:
        messages.error(request, "You are not registered as a travel agent.")
        return redirect("register_agent")


@agent_required
def send_custom_confirmation_email(request, uuid):
    """Send confirmation email to a custom recipient for travel agents."""
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Invalid request method"})
    
    try:
        # Get the reservation and verify it belongs to the agent
        reservation = get_object_or_404(Reservation, uuid=uuid)
        travel_agent = TravelAgent.objects.get(user=request.user)
        
        if reservation.travel_agent != travel_agent:
            return JsonResponse({"success": False, "error": "Permission denied"})
        
        # Get the recipient email from the request
        recipient_email = request.POST.get("recipient_email")
        if not recipient_email:
            return JsonResponse({"success": False, "error": "Recipient email is required"})
        
        # Import the email function
        from .emails import send_reservation_confirmation_custom_recipient
        
        # Send the email
        success = send_reservation_confirmation_custom_recipient(
            reservation=reservation,
            recipient_email=recipient_email,
            sender_name=travel_agent.agent_name
        )
        
        if success:
            # Log the action in private notes
            timestamp = timezone.now().strftime("%Y-%m-%d %H:%M")
            note_addition = (
                f"\n[{timestamp}] Custom confirmation email sent to {recipient_email} by {request.user.username}"
            )

            if reservation.private_notes:
                reservation.private_notes += note_addition
            else:
                reservation.private_notes = note_addition

            reservation.save(update_fields=["private_notes"])
            
            messages.success(request, f"Confirmation email sent successfully to {recipient_email}")
            return JsonResponse({"success": True, "message": f"Email sent to {recipient_email}"})
        else:
            return JsonResponse({"success": False, "error": "Failed to send email"})
            
    except Exception as e:
        logger.error(f"Error sending custom confirmation email: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)})


# =============================================
# AGENCY VIEWS & CLASSES
# =============================================


def is_agency_head(user):
    """Check if user is a head of any agency."""
    if not user.is_authenticated:
        return False
    return Agency.objects.filter(heads=user).exists()


def get_user_agencies(user):
    """Get all agencies where the user is a head."""
    if not user.is_authenticated:
        return Agency.objects.none()
    return Agency.objects.filter(heads=user)


class AgencyDashboardView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    """
    Main dashboard for agency heads. Shows either single agency detail view
    or multi-agency summary based on how many agencies the user manages.
    """

    model = Agency
    template_name = "users/agency_dashboard.html"
    context_object_name = "agencies"

    def test_func(self):
        """Only allow users who are heads of at least one agency."""
        return Agency.objects.filter(heads=self.request.user).exists()

    def get_queryset(self):
        """Get all agencies managed by the current user with statistics."""
        return (
            Agency.objects.filter(heads=self.request.user)
            .prefetch_related("agents", "heads", "agents__user", "agents__reservations")
            .annotate(
                total_agents=Count("agents"),
                total_unpaid=Sum("agents__unpaid_commissions"),
                total_pending=Sum("agents__pending_commissions"),
                total_paid=Sum("agents__total_paid_commission"),
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agencies = self.get_queryset()

        if agencies.count() == 1:
            # Single agency - detailed view
            agency = agencies.first()

            recent_reservations = (
                Reservation.objects.filter(travel_agent__in=agency.agents.all())
                .select_related("customer", "vehicle", "travel_agent")
                .order_by("-created_at")[:10]
            )

            recent_payouts = (
                CommissionPayout.objects.filter(agent__in=agency.agents.all())
                .select_related("agent")
                .order_by("-paid_at")[:8]
            )

            # Calculate agent statistics
            agents_with_stats = [
                {
                    "agent": agent,
                    "unpaid": format_decimal(agent.unpaid_commissions or 0),
                    "pending": format_decimal(agent.pending_commissions or 0),
                    "paid": format_decimal(agent.total_paid_commission or 0),
                    "reservation_count": agent.reservations.count(),
                }
                for agent in agency.agents.all()
            ]

            context.update(
                {
                    "agency": agency,
                    "agents": agency.agents.all(),
                    "total_unpaid": format_decimal(agency.total_unpaid or 0),
                    "total_pending": format_decimal(agency.total_pending or 0),
                    "total_paid": format_decimal(agency.total_paid or 0),
                    "recent_reservations": recent_reservations,
                    "recent_payouts": recent_payouts,
                    "agents_with_stats": agents_with_stats,
                }
            )
        else:
            # Multiple agencies - summary view
            context["agencies_with_stats"] = [
                {
                    "agency": agency,
                    "agent_count": agency.total_agents,
                    "unpaid": format_decimal(agency.total_unpaid or 0),
                    "pending": format_decimal(agency.total_pending or 0),
                    "paid": format_decimal(agency.total_paid or 0),
                }
                for agency in agencies
            ]

        return context


@login_required
def agency_commission_history(request, agency_id):
    """
    Display comprehensive commission history for an agency including:
    - Main agency payouts (payments made to the agency)
    - Individual agent payouts (breakdown by agent)
    - Unpaid commissions ready for payment
    """
    # Verify user permissions
    agency = get_object_or_404(Agency.objects.prefetch_related("heads"), id=agency_id)
    if not agency.heads.filter(id=request.user.id).exists():
        raise Http404("You don't have permission to view this agency")

    # Get main agency payouts with optimized queries
    agency_payouts = (
        AgencyCommissionPayout.objects.filter(agency=agency)
        .prefetch_related(
            Prefetch(
                "agent_payouts",
                queryset=CommissionPayout.objects.select_related(
                    "agent", "agent__user"
                ).prefetch_related(
                    Prefetch(
                        "reservations",
                        queryset=Reservation.objects.select_related(
                            "customer", "vehicle"
                        ),
                    )
                ),
            )
        )
        .order_by("-paid_at")
    )

    # Calculate total reservations for each agency payout
    for payout in agency_payouts:
        payout.total_reservations = sum(
            agent_payout.reservations.count()
            for agent_payout in payout.agent_payouts.all()
        )

    # Get individual agent payouts with optimized query
    payouts = (
        CommissionPayout.objects.filter(agent__in=agency.agents.all())
        .select_related("agent", "agent__user")
        .prefetch_related(
            Prefetch(
                "reservations",
                queryset=Reservation.objects.select_related(
                    "customer", "vehicle", "travel_agent"
                ),
            )
        )
        .order_by("-paid_at")
    )

    # Get unpaid reservations with optimized query
    unpaid_reservations = (
        Reservation.objects.filter(
            travel_agent__in=agency.agents.all(),
            commission_paid=False,
            status="completed",
        )
        .select_related("customer", "vehicle", "travel_agent", "travel_agent__user")
        .order_by("-created_at")
    )

    # Calculate totals using aggregation
    totals = agency.agents.aggregate(
        total_paid=Sum("total_paid_commission"), total_unpaid=Sum("unpaid_commissions")
    )

    # Paginate individual payouts
    paginator = Paginator(payouts, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "agency": agency,
        "agency_payouts": agency_payouts,
        "payouts": page_obj,
        "unpaid_reservations": unpaid_reservations,
        "total_paid": format_decimal(totals["total_paid"] or 0),
        "total_unpaid": format_decimal(totals["total_unpaid"] or 0),
    }

    return render(request, "users/agency_commission_history.html", context)


class AgencyDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    """Detailed view of a specific agency for agency heads."""

    model = Agency
    template_name = "users/agency_detail.html"
    context_object_name = "agency"

    def test_func(self):
        """Only allow agency heads to access this view."""
        agency = self.get_object()
        return agency.heads.filter(id=self.request.user.id).exists()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agency = self.get_object()
        agents = agency.agents.all()

        # Commission statistics
        context.update(
            {
                "agents": agents,
                "total_unpaid": format_decimal(agency.get_total_unpaid_commissions()),
                "total_pending": format_decimal(agency.get_total_pending_commissions()),
                "total_paid": format_decimal(agency.get_total_paid_commissions()),
            }
        )

        # Recent activity
        context["recent_reservations"] = Reservation.objects.filter(
            travel_agent__in=agents
        ).order_by("-created_at")[:10]
        context["recent_payouts"] = CommissionPayout.objects.filter(
            agent__in=agents
        ).order_by("-paid_at")[:10]

        # Agent statistics
        context["agents_with_stats"] = [
            {
                "agent": agent,
                "unpaid": format_decimal(agent.unpaid_commissions),
                "pending": format_decimal(agent.pending_commissions),
                "paid": format_decimal(agent.total_paid_commission),
                "reservation_count": Reservation.objects.filter(
                    travel_agent=agent
                ).count(),
            }
            for agent in agents
        ]

        return context


class AgentDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    """Detailed view of a specific agent for agency heads or the agent themselves."""

    model = TravelAgent
    template_name = "users/agent_detail.html"
    context_object_name = "agent"

    def test_func(self):
        """Allow access to agency heads or the agent themselves."""
        agent = self.get_object()
        if self.request.user == agent.user:
            return True
        if agent.agency and agent.agency.heads.filter(id=self.request.user.id).exists():
            return True
        return False

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agent = self.get_object()

        # Get reservations and statistics
        reservations = Reservation.objects.filter(travel_agent=agent).order_by(
            "-created_at"
        )
        context["reservations"] = reservations

        # Status statistics with formatted values
        status_stats = reservations.values("status").annotate(
            count=Count("id"),
            total_price=Sum("total_price"),
            commission=Sum("commission_amount"),
        )

        for stat in status_stats:
            if stat.get("total_price"):
                stat["total_price"] = format_decimal(stat["total_price"])
            if stat.get("commission"):
                stat["commission"] = format_decimal(stat["commission"])

        context["status_stats"] = status_stats

        # Commission payouts
        context["payouts"] = CommissionPayout.objects.filter(agent=agent).order_by(
            "-paid_at"
        )

        # Check if current user is agency head
        context["is_agency_head"] = (
            agent.agency and agent.agency.heads.filter(id=self.request.user.id).exists()
        )

        return context


class AgencyAgentsListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    """List view of all agents in an agency."""

    model = TravelAgent
    template_name = "users/agency_agents_list.html"
    context_object_name = "agents"

    def test_func(self):
        """Only allow agency heads to access this view."""
        agency = get_object_or_404(Agency, pk=self.kwargs["pk"])
        return agency.heads.filter(id=self.request.user.id).exists()

    def get_queryset(self):
        """Get all agents for this agency."""
        agency = get_object_or_404(Agency, pk=self.kwargs["pk"])
        return TravelAgent.objects.filter(agency=agency)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agency = get_object_or_404(Agency, pk=self.kwargs["pk"])
        context["agency"] = agency

        # Format decimal values for display
        for agent in context["agents"]:
            agent.total_paid_commission = format_decimal(agent.total_paid_commission)
            agent.unpaid_commissions = format_decimal(agent.unpaid_commissions)
            agent.pending_commissions = format_decimal(agent.pending_commissions)

        return context


class AgencyProfileView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    """View for editing agency profile information."""

    model = Agency
    template_name = "users/agency_profile.html"
    fields = ["name", "phone", "address", "website", "logo"]
    success_url = reverse_lazy("agency_profile")

    def test_func(self):
        """Only allow agency heads to access this view."""
        return Agency.objects.filter(heads=self.request.user).exists()

    def get_object(self, queryset=None):
        """Get the agency where the user is a head."""
        return get_object_or_404(Agency, heads=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agency = self.get_object()

        context.update(
            {
                "payment_methods": TravelAgent.PAYMENT_METHOD_CHOICES,
                "agents": agency.agents.all(),
                "total_paid": format_decimal(agency.get_total_paid_commissions()),
                "total_pending": format_decimal(agency.get_total_pending_commissions()),
                "total_unpaid": format_decimal(agency.get_total_unpaid_commissions()),
            }
        )

        return context

    def form_valid(self, form):
        """Handle successful form submission."""
        messages.success(self.request, "Agency profile updated successfully.")
        return super().form_valid(form)


class AgencyGuideView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """View for agency management guide and documentation."""

    template_name = "users/agency_guide.html"

    def test_func(self):
        """Only allow agency heads to access this view."""
        return Agency.objects.filter(heads=self.request.user).exists()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["agency"] = get_object_or_404(Agency, heads=self.request.user)
        return context


# =============================================
# COMMISSION & PAYOUT VIEWS
# =============================================


@login_required
def commission_payout_detail(request, pk):
    """
    View details of a specific commission payout.
    Accessible by the agent themselves or their agency heads.
    """
    payout = get_object_or_404(CommissionPayout, pk=pk)
    agent = payout.agent

    # Check permissions
    has_permission = request.user == agent.user or (
        agent.agency and agent.agency.heads.filter(id=request.user.id).exists()
    )

    if not has_permission:
        messages.error(request, "Permission denied.")
        return redirect("home")

    # Format values for display
    reservations = payout.reservations.all().order_by("-created_at")
    for reservation in reservations:
        if hasattr(reservation, "total_price"):
            reservation.total_price = format_decimal(reservation.total_price)
        if hasattr(reservation, "commission_amount"):
            reservation.commission_amount = format_decimal(
                reservation.commission_amount
            )

    context = {
        "payout": payout,
        "formatted_amount": format_decimal(payout.total_amount),
        "agent": agent,
        "reservations": reservations,
        "is_agency_head": (
            agent.agency and agent.agency.heads.filter(id=request.user.id).exists()
        ),
    }

    return render(request, "users/commission_payout_detail.html", context)


@login_required
def update_agency_payment(request):
    """Update agency payment information."""
    if request.method == "POST":
        agency = get_object_or_404(Agency, heads=request.user)

        agency.payment_method = request.POST.get("payment_method")
        agency.payment_info = request.POST.get("payment_info")
        agency.save()

        messages.success(request, "Payment information updated successfully.")
        return redirect("agency_profile")

    return redirect("agency_dashboard")


@login_required
def agency_commission_payout_detail(request, payout_id):
    """Display detailed information about a specific agency commission payout."""
    payout = get_object_or_404(AgencyCommissionPayout, id=payout_id)
    agency = payout.agency

    # Verify user permissions
    if not agency.heads.filter(id=request.user.id).exists():
        raise Http404("You don't have permission to view this payout")

    # Calculate total reservations across all agent payouts
    total_reservations = sum(
        agent_payout.reservations.count() for agent_payout in payout.agent_payouts.all()
    )

    # Calculate average commission per agent
    average_commission = (
        payout.total_amount / payout.agent_payouts.count()
        if payout.agent_payouts.exists()
        else 0
    )

    context = {
        "agency": agency,
        "payout": payout,
        "total_reservations": total_reservations,
        "average_commission": average_commission,
    }

    return render(request, "users/agency_commission_detail.html", context)


class AdminRequiredMixin(UserPassesTestMixin):
    """Mixin to ensure only superusers can access the view."""

    def test_func(self):
        return self.request.user.is_superuser


class AgencyListView(AdminRequiredMixin, ListView):
    """Simple list view of all agencies."""

    model = Agency
    template_name = "users/agency_list.html"
    context_object_name = "agencies"

    def get_queryset(self):
        return Agency.objects.all().prefetch_related("agents")


class AgencyDetailView(AdminRequiredMixin, DetailView):
    """Detailed view of a specific agency."""

    model = Agency
    template_name = "users/agency_detail.html"
    context_object_name = "agency"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agency = self.get_object()
        context.update(
            {
                "agents": agency.agents.all(),
                "total_agents": agency.agents.count(),
                "total_unpaid": format_decimal(agency.get_total_unpaid_commissions()),
                "total_pending": format_decimal(agency.get_total_pending_commissions()),
                "total_paid": format_decimal(agency.get_total_paid_commissions()),
            }
        )
        return context


class AgencyUpdateView(AdminRequiredMixin, UpdateView):
    """View for updating agency information."""

    model = Agency
    template_name = "users/agency_form.html"
    fields = ["name", "phone", "address", "website", "logo", "is_active"]
    success_url = reverse_lazy("agency_list")

    def form_valid(self, form):
        messages.success(self.request, "Agency updated successfully.")
        return super().form_valid(form)
