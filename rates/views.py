from django.shortcuts import render, redirect
from django.urls import reverse
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.db.models import Prefetch
import json
import logging
from decimal import Decimal

from .models import Vehicle, Location, Rate, Route, Lead
from .forms import LeadForm

logger = logging.getLogger(__name__)


def index(request):
    vehicles = Vehicle.objects.prefetch_related(
        Prefetch(
            "rates",
            queryset=Rate.objects.select_related(
                "route", "route__origin", "route__destination"
            ),
        )
    ).all()
    structured_data = {
        "@type": "Offer",
        "description": "Comprehensive transportation rates for Orlando airport, Disney, and Universal transfers",
    }
    context = {"vehicles": vehicles, "additional_data": structured_data}
    return render(request, "rates/index.html", context)


def quote(request):
    """
    Main view for displaying the quote form and handling form submissions.
    """
    # Get all vehicles, locations, and rates with efficient prefetching
    vehicles = Vehicle.objects.prefetch_related(
        Prefetch("rates", queryset=Rate.objects.select_related("route", "route__origin", "route__destination"))
    ).all()
    
    locations = Location.objects.all()
    
    # Initialize an empty form
    form = LeadForm()
    
    # Prepare data for frontend
    rates_json = {}
    for vehicle in vehicles:
        routes = {}
        for rate in vehicle.rates.all():
            route = rate.route
            routes[str(rate.id)] = {
                "id": rate.id,
                "name": str(route),
                "origin_id": route.origin_id,
                "destination_id": route.destination_id,
                "origin_name": route.origin.name,
                "destination_name": route.destination.name,
                "oneway": float(rate.oneway_price),
                "round": float(rate.round_trip_price),
                "reserve_url": reverse("reserve", args=[rate.id]),
            }
        rates_json[str(vehicle.id)] = routes
    
    # Prepare locations for frontend
    locations_json = {str(loc.id): {"id": loc.id, "name": loc.name} for loc in locations}
    
    # Prepare routes for frontend
    routes = Route.objects.all().select_related('origin', 'destination')
    populated_routes = [
        {
            "origin_id": route.origin_id,
            "destination_id": route.destination_id,
            "origin_name": route.origin.name,
            "destination_name": route.destination.name,
        }
        for route in routes
    ]
    
    # Context for template
    context = {
        "vehicles": vehicles,
        "locations": locations,
        "rates_json": json.dumps(rates_json),
        "locations_json": json.dumps(locations_json),
        "populated_routes": json.dumps(populated_routes),
        "form": form,
    }
    
    return render(request, "rates/test-quote.html", context)


@require_http_methods(["POST"])
def save_lead(request):
    """
    API endpoint for saving leads from the quote form.
    This is called when the user clicks 'Get Quote' button in step 3.
    """
    # Check if this is an AJAX request
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    # Log the incoming POST data for debugging
    logger.debug(f"Save lead request received with data: {request.POST}")
    
    form = LeadForm(request.POST)
    if form.is_valid():
        try:
            # Create lead but don't save yet
            lead = form.save(commit=False)
            
            # Try to get the route if it exists
            if lead.origin and lead.destination:
                try:
                    lead.route = Route.objects.get(origin=lead.origin, destination=lead.destination)
                except Route.DoesNotExist:
                    # Try reverse direction
                    try:
                        lead.route = Route.objects.get(origin=lead.destination, destination=lead.origin)
                    except Route.DoesNotExist:
                        logger.warning(f"No route found for origin {lead.origin} and destination {lead.destination}")
            
            # Save the lead
            lead.save()
            
            logger.info(f"Lead saved successfully with ID: {lead.id}")
            
            # Get the return URL if provided
            return_url = request.POST.get('return_url', '')
            
            # For AJAX requests, return JSON response
            if is_ajax:
                return JsonResponse({
                    "success": True, 
                    "lead_id": lead.id,
                    "message": "Thank you! Your information has been saved.",
                    "return_url": return_url
                })
            
            # For regular form submissions, redirect to return URL if provided
            if return_url:
                return redirect(return_url)
            
            # Otherwise return to the quote page with success message
            return redirect('quote')
            
        except Exception as e:
            logger.error(f"Error saving lead: {str(e)}")
            if is_ajax:
                return JsonResponse({
                    "success": False, 
                    "errors": {"__all__": [f"Error saving lead: {str(e)}"]}
                })
            # For regular form submissions
            form.add_error(None, f"Error saving lead: {str(e)}")
    else:
        logger.warning(f"Lead form validation failed: {form.errors}")
        if is_ajax:
            return JsonResponse({"success": False, "errors": form.errors})
    
    # If we get here with a regular form submission, there was an error
    # Rerender the quote page with the form errors
    return render(request, "rates/test-quote.html", {"form": form})