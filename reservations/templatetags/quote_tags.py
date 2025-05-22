# reservations/templatetags/quote_tags.py
import json
import logging
from django import template
from django.db.models import Prefetch
from django.urls import reverse
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_http_methods

from ..models import Vehicle, Rate
from ..forms import LeadForm

register = template.Library()
logger = logging.getLogger(__name__)

@register.inclusion_tag('reservations/quote_form.html', takes_context=True)
def quote_form(context, form_id="quote-form", css_classes=""):
    """
    Renders a quote form with vehicle selection and rate calculation.
    
    Usage:
    {% load quote_tags %}
    {% quote_form %}
    
    Or with custom parameters:
    {% quote_form form_id="my-quote-form" css_classes="my-custom-class" %}
    """
    # Get vehicles with prefetched rates
    vehicles = Vehicle.objects.prefetch_related(
        Prefetch(
            "rates",
            queryset=Rate.objects.select_related(
                "route", "route__origin", "route__destination"
            ),
        )
    ).all()
    
    # Get all unique locations from routes
    locations = set()
    for vehicle in vehicles:
        for rate in vehicle.rates.all():
            locations.add(rate.route.origin)
            locations.add(rate.route.destination)
    
    # Create rates map with location pairs as keys
    rates_json = {}
    for v in vehicles:
        routes = {}
        for r in v.rates.all():
            # Create a key that's direction-agnostic by sorting origin and destination IDs
            location_ids = sorted([str(r.route.origin.id), str(r.route.destination.id)])
            key = f"{location_ids[0]}-{location_ids[1]}"
            
            routes[key] = {
                "id": r.id,
                "name": str(r.route),
                "origin": str(r.route.origin),
                "destination": str(r.route.destination),
                "oneway": float(r.oneway_price),
                "round": float(r.round_trip_price),
                "reserve_url": reverse("reserve", args=[r.id]),
            }
        rates_json[str(v.id)] = routes
    
    return {
        'vehicles': vehicles,
        'locations': sorted(locations, key=lambda x: x.name),
        'rates_json': json.dumps(rates_json),
        'form': LeadForm(),
        'form_id': form_id,
        'css_classes': css_classes,
        'quote_endpoint_url': reverse('quote_form_handler'),
        'request': context['request'],
    }

@register.simple_tag
def quote_form_scripts():
    """
    Returns the JavaScript code needed for the quote form functionality.
    Include this in your template after the quote_form tag.
    
    Usage:
    {% quote_form_scripts %}
    """
    return """
    <script>
      // Quote form JavaScript will be loaded here
      // This ensures the script is only loaded when the tag is used
    </script>
    """