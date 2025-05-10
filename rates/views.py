from django.shortcuts import render
from .models import Rate, Vehicle, Location
from django.db.models import Prefetch
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
import json
import logging

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


# def quote_page(request):
#     """Render the quote page with available locations and vehicles."""
#     locations = Location.objects.all()
#     vehicles = Vehicle.objects.all()
#     context = {
#         'locations': locations,
#         'vehicles': vehicles,
#     }
#     return render(request, "rates/quote.html", context)

# @require_http_methods(["POST"])
# def get_quote(request):
#     """Handle AJAX request to get a quote based on selected options."""
#     try:
#         data = json.loads(request.body)
#         origin_id = data.get('origin')
#         destination_id = data.get('destination')
#         vehicle_id = data.get('vehicle')
#         trip_type = data.get('tripType')

#         logger.info(f"Quote request - Origin: {origin_id}, Destination: {destination_id}, Vehicle: {vehicle_id}, Trip Type: {trip_type}")

#         # Get location names for better error messages
#         try:
#             origin = Location.objects.get(id=origin_id)
#             destination = Location.objects.get(id=destination_id)
#             vehicle = Vehicle.objects.get(id=vehicle_id)
#         except (Location.DoesNotExist, Vehicle.DoesNotExist):
#             return JsonResponse({
#                 'success': False,
#                 'error': 'Invalid selection. Please try again.',
#                 'show_contact': False
#             })

#         # Try to get the rate in both directions
#         try:
#             # First try origin -> destination
#             rate = Rate.objects.select_related('route').get(
#                 route__origin_id=origin_id,
#                 route__destination_id=destination_id,
#                 vehicle_id=vehicle_id
#             )
#             logger.info(f"Found rate: {rate}")
#         except Rate.DoesNotExist:
#             try:
#                 # If not found, try destination -> origin
#                 rate = Rate.objects.select_related('route').get(
#                     route__origin_id=destination_id,
#                     route__destination_id=origin_id,
#                     vehicle_id=vehicle_id
#                 )
#                 logger.info(f"Found rate in reverse direction: {rate}")
#             except Rate.DoesNotExist:
#                 logger.warning(f"No rate found for Origin: {origin_id}, Destination: {destination_id}, Vehicle: {vehicle_id}")
#                 return JsonResponse({
#                     'success': False,
#                     'error': f'For {vehicle.get_vehicle_type_display()} service from {origin.name} to {destination.name}, please call our office at (407) 212-7190 for a custom quote.',
#                     'show_contact': True,
#                     'phone': '407-212-7190'
#                 })

#         # Determine price based on trip type
#         price = rate.round_trip_price if trip_type == 'roundtrip' else rate.oneway_price

#         # Generate booking URL
#         booking_url = f"/reserve/{rate.id}?round={'2' if trip_type == 'roundtrip' else '1'}"

#         return JsonResponse({
#             'success': True,
#             'price': str(price),
#             'booking_url': booking_url,
#             'route': str(rate.route),
#             'vehicle': vehicle.get_vehicle_type_display()
#         })
#     except Exception as e:
#         logger.error(f"Error in get_quote: {str(e)}", exc_info=True)
#         return JsonResponse({
#             'success': False,
#             'error': 'An unexpected error occurred. Please try again or call our office at (407) 212-7190.',
#             'show_contact': True,
#             'phone': '407-212-7190'
#         })
