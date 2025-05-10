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
from .models import NewsLetter, NewsletterSubscriptionAttempt, TravelAgent
from reservations.models import Reservation
from django.contrib.auth.models import User


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
                return redirect("schedule")
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
            thankyou_email(instance)
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
            thankyou_email(instance)
            return redirect("thankyou")
    else:
        form = ContactUsFormSubmission()
    context = {"form": form}

    return render(request, "reservations/contact.html", context)


def newsletter_subscribe(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        if email:
            # Get client IP
            x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
            if x_forwarded_for:
                ip_address = x_forwarded_for.split(',')[0]
            else:
                ip_address = request.META.get('REMOTE_ADDR')
            
            # Record the attempt
            attempt = NewsletterSubscriptionAttempt.objects.create(
                ip_address=ip_address,
                email=email
            )
            
            # Check if email already exists
            if not NewsLetter.objects.filter(email=email).exists():
                NewsLetter.objects.create(email=email)
                attempt.success = True
                attempt.save()
                messages.success(request, 'Thank you for subscribing to our newsletter!', extra_tags='newsletter_success')
            else:
                messages.info(request, 'You are already subscribed to our newsletter!', extra_tags='newsletter_info')
        else:
            messages.error(request, 'Please provide a valid email address.', extra_tags='newsletter_error')
    
    # Redirect back to the previous page
    return redirect(request.META.get('HTTP_REFERER', '/'))


def register_agent(request):
    if request.method == 'POST':
        # Get form data
        username = request.POST.get('username')
        email = request.POST.get('email')
        password1 = request.POST.get('password1')
        password2 = request.POST.get('password2')
        agent_name = request.POST.get('agent_name')
        agency_name = request.POST.get('agency_name')
        agency_email = request.POST.get('agency_email')
        phone = request.POST.get('phone')
        payment_info = request.POST.get('payment_info')
        
        # Validate passwords match
        if password1 != password2:
            messages.error(request, 'Passwords do not match.')
            context = {
                'form_data': {
                    'username': username,
                    'email': email,
                    'agent_name': agent_name,
                    'agency_name': agency_name,
                    'agency_email': agency_email,
                    'phone': phone,
                    'payment_info': payment_info
                }
            }
            return render(request, 'users/register_agent.html', context)
            
        # Check if username or email already exists
        if User.objects.filter(username=username).exists():
            messages.error(request, 'Username already exists.')
            context = {
                'form_data': {
                    'username': username,
                    'email': email,
                    'agent_name': agent_name,
                    'agency_name': agency_name,
                    'agency_email': agency_email,
                    'phone': phone,
                    'payment_info': payment_info
                }
            }
            return render(request, 'users/register_agent.html', context)
            
        if User.objects.filter(email=email).exists():
            messages.error(request, 'Email already exists.')
            context = {
                'form_data': {
                    'username': username,
                    'email': email,
                    'agent_name': agent_name,
                    'agency_name': agency_name,
                    'agency_email': agency_email,
                    'phone': phone,
                    'payment_info': payment_info
                }
            }
            return render(request, 'users/register_agent.html', context)
            
        try:
            # Create user account
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password1
            )
            
            # Create travel agent profile
            TravelAgent.objects.create(
                user=user,
                agent_name=agent_name,
                agency_name=agency_name,
                agency_email=agency_email,
                phone=phone,
                payment_info=payment_info
            )
            
            # Log them in
            login(request, user)
            messages.success(request, 'Successfully registered as a travel agent!')
            return redirect('agent_dashboard')
            
        except Exception as e:
            messages.error(request, f'Error creating account: {str(e)}')
            context = {
                'form_data': {
                    'username': username,
                    'email': email,
                    'agent_name': agent_name,
                    'agency_name': agency_name,
                    'agency_email': agency_email,
                    'phone': phone,
                    'payment_info': payment_info
                }
            }
            return render(request, 'users/register_agent.html', context)
            
    return render(request, 'users/register_agent.html')


@login_required
def agent_dashboard(request):
    try:
        travel_agent = TravelAgent.objects.get(user=request.user)
        
        # Get status filter from query params
        status = request.GET.get('status', 'all')
        
        # Base queryset
        reservations = Reservation.objects.filter(travel_agent=travel_agent)
        
        # Apply status filter
        if status == 'pending':
            reservations = reservations.filter(status='pending')
        elif status == 'completed':
            reservations = reservations.filter(status='completed')
            
        # Get counts for stats
        pending_count = Reservation.objects.filter(travel_agent=travel_agent, status='pending').count()
        completed_count = Reservation.objects.filter(travel_agent=travel_agent, status='completed').count()
        
        # Calculate total commission
        total_commission = sum(r.commission_amount or 0 for r in reservations)
        
        context = {
            'travel_agent': travel_agent,
            'reservations': reservations.order_by('-created_at'),
            'total_commission': total_commission,
            'pending_count': pending_count,
            'completed_count': completed_count,
            'status': status
        }
        return render(request, 'users/agent_dashboard.html', context)
    except TravelAgent.DoesNotExist:
        messages.error(request, 'You are not registered as a travel agent.')
        return redirect('register_agent')
