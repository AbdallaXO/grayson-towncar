from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from reservations.models import Reservation, Leg
from django.shortcuts import get_object_or_404
from reservations.forms import ReservationAdminForm, CustomerForm
from datetime import datetime
from django.core.paginator import Paginator
from django.db.models import Sum
from django.contrib import messages
from django import forms
from django.utils import timezone
from reservations.forms import LegForm
from django.shortcuts import redirect
import logging
from drivers.models import Driver
from django.http import JsonResponse
from django.views.decorators.http import require_POST
import json
from django.db.models import Q

logger = logging.getLogger(__name__)


class DateForm(forms.Form):
    date = forms.DateField(widget=forms.SelectDateWidget)


@login_required(login_url="login")
def index(request):
    """
    Dispatcher Dashboard Shows All Legs-Lets you Filter by Date
    """
    if not request.user.is_superuser:
        return redirect("home")

    selected_date = request.GET.get("date")
    try:
        selected_date = (
            datetime.strptime(selected_date, "%Y-%m-%d").date()
            if selected_date
            else timezone.localdate()
        )
    except ValueError:
        selected_date = timezone.localdate()
    legs = (
        Leg.objects.filter(pickup_date=selected_date)
        .select_related(
            "reservation",
            "reservation__customer",
            "reservation__vehicle",
            "flight_information",
        )
        .order_by("pickup_time")
    )
    context = {
        "legs": legs,
        "selected_date": selected_date,
        "total_legs": legs.count(),
        "total_revenue": sum(leg.reservation.total_price for leg in legs) / 2,
    }

    return render(request, "dispatching/legs_filter.html", context)


@login_required(login_url="login")
def all_reservations(request):
    if not request.user.is_superuser:
        return redirect("home")
    """
    List all reservations with pagination and overview statistics
    """
    search = ''
    if request.GET.get('search_q'):
        search = request.GET.get('search_q')
    
    reservations_query = Reservation.objects.select_related(
        "customer", "rate", "vehicle"
    ).order_by("legs__pickup_date").filter(Q(customer__first_name__icontains=search) & ~Q(status='completed'))
    
    paginator = Paginator(reservations_query, 10)  
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    total_reservations = reservations_query.count()
    pending_reservations = reservations_query.filter(status="pending").count()
    confirmed_reservations = reservations_query.filter(status="confirmed").count()
    total_revenue = reservations_query.aggregate(total=Sum("total_price"))["total"] or 0    
    context = {
        "reservations": page_obj,
        "page_obj": page_obj,
        "total_reservations": total_reservations,
        "pending_reservations": pending_reservations,
        "confirmed_reservations": confirmed_reservations,
        "total_revenue": total_revenue,
    }

    return render(request, "dispatching/all_reservations.html", context)


@login_required(login_url="login")
def reservation_details(request, id):
    if not request.user.is_superuser:
        return redirect("home")
    """
    Detailed view for a reservation
    """
    reservation = get_object_or_404(
        Reservation.objects.prefetch_related("legs__flight_information").select_related(
            "customer", "vehicle", "rate"
        ),
        uuid=id,
    )

    context = {
        "reservation": reservation,
        "total_legs": reservation.legs.count(),
        "total_cost": {
            "base": reservation.base_price,
            "additional": reservation.additional_charges,
            "total": reservation.total_price,
        },
    }

    return render(request, "dispatching/reservation_view.html", context)


@login_required(login_url="login")
def modify_reservation(request, id):
    if not request.user.is_superuser:
        return redirect("home")
    reservation = get_object_or_404(
        Reservation.objects.prefetch_related("legs"), uuid=id
    )

    if request.method == "POST":
        customer_form = CustomerForm(request.POST, instance=reservation.customer)
        reservation_form = ReservationAdminForm(request.POST, instance=reservation)

        if customer_form.is_valid() and reservation_form.is_valid():
            # Save customer
            customer = customer_form.save()
            
            # Save reservation with commit=False first
            updated_reservation = reservation_form.save(commit=False)
            
            updated_reservation.customer = customer
            # Save the reservation
            updated_reservation.save()
            
            # Process leg forms
            leg_forms = []
            for i in range(1, 3):  # Support up to 2 legs
                leg_prefix = f"leg_{i}"
                
                # Create a dictionary with all possible leg form fields
                leg_data = {}
                for field in request.POST:
                    if field.startswith(leg_prefix):
                        leg_data[field] = request.POST.get(field)
                
                # Check if any meaningful data was submitted
                has_data = False
                for key, value in leg_data.items():
                    if value and not key.endswith('-id'):  # Ignore empty values and ID fields
                        has_data = True
                        break
                
                if has_data:
                    leg_instance = (
                        reservation.legs.all()[i - 1]
                        if reservation.legs.count() >= i
                        else None
                    )
                    leg_form = LegForm(
                        request.POST, instance=leg_instance, prefix=leg_prefix
                    )
                    if leg_form.is_valid():
                        leg = leg_form.save(commit=False)
                        leg.reservation = updated_reservation
                        leg.save()

            messages.success(
                request, f"Reservation {updated_reservation.uuid} updated successfully."
            )
            return redirect("reservation_details", id=updated_reservation.uuid)
        else:
            messages.error(request, "Please correct the errors in the form.")
    else:
        customer_form = CustomerForm(instance=reservation.customer)
        reservation_form = ReservationAdminForm(instance=reservation)
        leg_forms = [
            LegForm(instance=leg, prefix=f"leg_{i + 1}")
            for i, leg in enumerate(reservation.legs.all())
        ]
        if not leg_forms:
            leg_forms.append(LegForm(prefix="leg_1"))

    context = {
        "reservation": reservation,
        "customer_form": customer_form,
        "reservation_form": reservation_form,
        "leg_forms": leg_forms,
    }

    return render(request, "dispatching/modify_reservation.html", context)


@login_required(login_url="login")
def legs_list(request):
    if not request.user.is_superuser:
        return redirect("home")
    date_filter = request.GET.get("date")
    today = timezone.localdate()

    legs_query = Leg.objects.select_related(
        "reservation", "reservation__customer", "reservation__vehicle"
    )
    legs_query = legs_query.filter(pickup_date__gte=today)
    today_count = legs_query.filter(pickup_date=today).count()

    if date_filter:
        try:
            filter_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
            legs_query = legs_query.filter(pickup_date=filter_date)
        except ValueError:
            pass

    # Order by pickup date first, then pickup time for better readability
    legs = legs_query.order_by("pickup_date", "pickup_time")

    drivers = Driver.objects.all()

    context = {"legs": legs, "filter_date": date_filter, "drivers": drivers, "today_count":today_count,}

    return render(request, "dispatching/legs_list.html", context)


@login_required
@require_POST
def update_leg_assignment(request):
    """
    Update a leg's driver assignment or status via AJAX.
    """
    logger.info("Received update_leg_assignment request")
    
    if not request.user.is_superuser:
        logger.warning(f"Permission denied for user {request.user.username}")
        return JsonResponse({'success': False, 'error': 'Permission denied'}, status=403)
    
    try:
        try:
            data = json.loads(request.body)
            logger.info(f"Received data: {data}")
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {str(e)}")
            return JsonResponse({'success': False, 'error': f'Invalid JSON: {str(e)}'}, status=400)
        
        leg_id = data.get('leg_id')
        field = data.get('field')
        value = data.get('value')
        
        logger.info(f"Processing request - leg_id: {leg_id}, field: {field}, value: {value}")
        
        if not leg_id or not field:
            logger.warning("Missing required fields")
            return JsonResponse({'success': False, 'error': 'Missing required data'}, status=400)
        
        # Get the leg
        try:
            leg = Leg.objects.get(id=leg_id)
            logger.info(f"Found leg for {leg.reservation}")
        except Leg.DoesNotExist:
            logger.warning(f"Leg with ID {leg_id} not found")
            return JsonResponse({'success': False, 'error': 'Leg not found'}, status=404)
        
        if field == 'driver':
            if value:
                try:
                    driver = Driver.objects.get(id=value)
                    logger.info(f"Found driver with ID {value}")
                    leg.driver = driver
                    leg.save()
                    logger.info(f"Updated leg {leg_id} with driver {driver.profile.username if hasattr(driver, 'profile') else driver.id}")
                except Driver.DoesNotExist:
                    logger.warning(f"Driver with ID {value} not found")
                    return JsonResponse({'success': False, 'error': 'Driver not found'}, status=404)
                except AttributeError as e:
                    logger.error(f"Attribute error: {str(e)} - check if driver has profile attribute")
                    return JsonResponse({'success': False, 'error': f'Driver profile error: {str(e)}'}, status=500)
                except Exception as e:
                    logger.error(f"Error updating driver: {str(e)}")
                    return JsonResponse({'success': False, 'error': f'Error updating driver: {str(e)}'}, status=500)
            else:
                leg.driver = None
                leg.save()
                logger.info(f"Removed driver from leg {leg_id}")
        elif field == 'status':
            try:
                # Update the LEG status, not the reservation status
                valid_statuses = ["in-progress", "picked-up", "completed"]
                if value in valid_statuses:
                    leg.status = value
                    leg.save()
                    logger.info(f"Updated leg {leg_id} status to {value}")
                else:
                    logger.warning(f"Invalid status value: {value}")
                    return JsonResponse({'success': False, 'error': f'Invalid status value: {value}'}, status=400)
            except Exception as e:
                logger.error(f"Error updating status: {str(e)}")
                return JsonResponse({'success': False, 'error': f'Error updating status: {str(e)}'}, status=500)
        else:
            logger.warning(f"Invalid field: {field}")
            return JsonResponse({'success': False, 'error': 'Invalid field'}, status=400)
        
        return JsonResponse({'success': True})
        
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return JsonResponse({'success': False, 'error': f'Server error: {str(e)}'}, status=500)