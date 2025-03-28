from django.views import View
from django.shortcuts import render, redirect, get_object_or_404
from reservations.models import Vehicle, Route, Rate, Reservation, Customer
from .forms import ReservationForm


# Create your views here.
def index(request):
    return render(request, "rates/index.html")


class BookRideView(View):
    def get(self, request):
        vehicle_type = request.GET.get("vehicle")
        route_id = request.GET.get("route")
        trip_type = request.GET.get("trip", "one_way")

        form = ReservationForm()
        context = {
            "form": form,
            "trip_type": trip_type,
        }

        # Only calculate and display price if both vehicle_type and route_id are provided
        if vehicle_type and route_id:
            try:
                route = get_object_or_404(Route, id=route_id)
                vehicle = get_object_or_404(Vehicle, vehicle_type=vehicle_type)
                rate = get_object_or_404(
                    Rate, vehicle__vehicle_type=vehicle_type, route=route
                )

                # Calculate price based on trip type
                if trip_type == "one_way":
                    base_price = rate.oneway_price
                else:
                    # Assuming round trip is 2x one-way price or there's a specific field
                    base_price = (
                        rate.round_trip_price
                        if hasattr(rate, "round_trip_price")
                        else rate.oneway_price * 2
                    )

                context.update(
                    {
                        "vehicle": vehicle,
                        "route": route,
                        "base_price": base_price,
                    }
                )
            except:
                # Handle the case where the specified vehicle/route combination doesn't exist
                context["error_message"] = (
                    "The selected vehicle and route combination is not available."
                )

        return render(request, "rates/book_form.html", context)

    def post(self, request):
        vehicle_type = request.GET.get("vehicle")
        route_id = request.GET.get("route")
        trip_type = request.GET.get("trip", "one_way")

        # Create a basic context that will be used in case of form errors
        context = {
            "trip_type": trip_type,
        }

        # Ensure we have the required parameters
        if not (vehicle_type and route_id):
            context["error_message"] = "Missing required vehicle or route information."
            return render(request, "rates/book_form.html", context)

        try:
            route = get_object_or_404(Route, id=route_id)
            vehicle = get_object_or_404(Vehicle, vehicle_type=vehicle_type)
            rate = get_object_or_404(
                Rate, vehicle__vehicle_type=vehicle_type, route=route
            )

            # Use the same price calculation logic as in get()
            if trip_type == "one_way":
                base_price = rate.oneway_price
            else:
                base_price = (
                    rate.round_trip_price
                    if hasattr(rate, "round_trip_price")
                    else rate.oneway_price * 2
                )

            form = ReservationForm(request.POST)
            if form.is_valid():
                reservation = form.save(commit=False)
                reservation.customer = (
                    Customer.objects.first()
                )  # Replace with logged-in logic
                reservation.route = route
                reservation.vehicle_type = vehicle
                reservation.base_price = base_price
                reservation.additional_charges = 0
                reservation.status = "PENDING"
                reservation.save()

                return redirect("home")

            # Update context with all necessary information if form is invalid
            context.update(
                {
                    "form": form,
                    "vehicle": vehicle,
                    "route": route,
                    "base_price": base_price,
                }
            )

        except:
            context["error_message"] = (
                "The selected vehicle and route combination is not available."
            )
            context["form"] = ReservationForm(request.POST)

        return render(request, "rates/book_form.html", context)
