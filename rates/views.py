from django.shortcuts import render
from .models import Rate, Vehicle, Location
from django.db.models import Prefetch, Case, When
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
            ).order_by(
                # First, prioritize Orlando International Airport
                Case(
                    When(route__origin__name="Orlando International Airport", then=0),
                    default=1
                ),
                # Then order by origin name, then destination name
                "route__origin__name",
                "route__destination__name"
            ),
        )
    ).all()
    structured_data = {
        "@type": "Offer",
        "description": "Comprehensive transportation rates for Orlando airport, Disney, and Universal transfers",
    }
    context = {"vehicles": vehicles, "additional_data": structured_data}
    return render(request, "rates/index.html", context)

