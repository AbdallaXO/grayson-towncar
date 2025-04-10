from django.http.response import (
    HttpResponse,
    HttpResponsePermanentRedirect,
    HttpResponseRedirect,
)
from django.shortcuts import render, get_object_or_404, redirect
from rates.models import Rate
from .forms import (
    ContactUsFormSubmission,
)
from .utils import (
    _initalize_form,
    get_form_details,
    returns_post_form,
    validate_forms,
    AIRLINES,
)

# Create your views here.
from .email import send_reservation_confirmation


def index(request):
    """Returns the Landing Page"""
    return render(request, "reservations/index.html")


def reservation_form(
    request, pk
) -> HttpResponsePermanentRedirect | HttpResponseRedirect | HttpResponse:
    """Returns a Reservation Form either oneway or roundtrip with a car type & rate & route or 404"""
    rate = get_object_or_404(Rate.objects.select_related("route", "vehicle"), pk=pk)
    trip_type, price = get_form_details(request, rate)
    if request.method == "POST":
        (
            customer_form,
            reservation_form,
            flight1_form,
            leg1_form,
            flight2_form,
            leg2_form,
        ) = returns_post_form(request, trip_type, rate)

        forms_valid = validate_forms(
            customer_form,
            reservation_form,
            flight1_form,
            leg1_form,
            flight2_form,
            leg2_form,
            trip_type,
        )
        if forms_valid:
            customer = customer_form.save()
            reservation = reservation_form.save(
                customer=customer,
                trip_type=trip_type,
                rate=rate,
                base_price=price,
                vehicle=rate.vehicle,
            )
            leg1 = leg1_form.save(commit=False)
            leg1.reservation = reservation

            if flight1_form and any(flight1_form.cleaned_data.values()):
                flight1 = flight1_form.save()
                leg1.flight_information = flight1
            else:
                leg1.flight_information = None
            leg1.save()

            if trip_type == "round_trip":
                leg2 = leg2_form.save(commit=False)
                leg2.reservation = reservation

                if flight2_form and any(flight2_form.cleaned_data.values()):
                    flight2 = flight2_form.save()
                    leg2.flight_information = flight2
                else:
                    leg2.flight_information = None
                leg2.save()
            return redirect("create_checkout_session", reservation_id=reservation.uuid)
    else:
        (
            customer_form,
            reservation_form,
            flight1_form,
            leg1_form,
            flight2_form,
            leg2_form,
        ) = _initalize_form(trip_type, rate, price)

    context = {
        "customer_form": customer_form,
        "reservation_form": reservation_form,
        "flight1_form": flight1_form,
        "flight2_form": flight2_form,
        "leg1_form": leg1_form,
        "leg2_form": leg2_form,
        "route": rate.route,
        "price": price,
        "trip_type": trip_type.replace("_", " "),
        "vehicle": rate.vehicle,
        "airlines": AIRLINES,
    }
    return render(request, "reservations/book_form.html", context)


def about_us(request):
    structured_data = {
        "@type": "AboutPage",
        "description": "Learn about Grayson Towncar's mission and commitment to transportation.",
    }
    return render(
        request, "reservations/about.html", {"structured_data": structured_data}
    )


# def confirm_reservation(request, pk):
#     """Shows reservation details for confirmation before payment"""
#     rate = get_object_or_404(Rate.objects.select_related("route", "vehicle"), pk=pk)

#     # Get form data from session
#     customer_data = request.session.get('customer_data', {})
#     reservation_data = request.session.get('reservation_data', {})
#     leg1_data = request.session.get('leg1_data', {})
#     flight1_data = request.session.get('flight1_data', {})
#     leg2_data = request.session.get('leg2_data', {})
#     flight2_data = request.session.get('flight2_data', {})
#     trip_type = request.session.get('trip_type')
#     base_price = request.session.get('base_price')

#     if request.method == "POST":
#         customer = Customer.objects.create(**customer_data)

#         reservation = Reservation.objects.create(
#             customer=customer,
#             trip_type=trip_type,
#             rate=rate,
#             base_price=base_price,
#             vehicle=rate.vehicle,
#             **reservation_data
#         )

#         leg1 = Leg.objects.create(
#             reservation=reservation,
#             **leg1_data
#         )

#         if flight1_data and any(flight1_data.values()):
#             flight1 = Flight.objects.create(**flight1_data)
#             leg1.flight_information = flight1
#             leg1.save()

#         if trip_type == "round_trip" and leg2_data:
#             leg2 = Leg.objects.create(
#                 reservation=reservation,
#                 **leg2_data
#             )

#             if flight2_data and any(flight2_data.values()):
#                 flight2 = Flight.objects.create(**flight2_data)
#                 leg2.flight_information = flight2
#                 leg2.save()
#         for key in ['customer_data', 'reservation_data', 'leg1_data',
#                    'flight1_data', 'leg2_data', 'flight2_data',
#                     'trip_type', 'base_price']:
#             request.session.pop(key, None)

#         return redirect("create_checkout_session", reservation_id=reservation.uuid)

#     # Context for confirmation template
#     context = {
#         'customer_data': customer_data,
#         'reservation_data': reservation_data,
#         'leg1_data': leg1_data,
#         'flight1_data': flight1_data,
#         'leg2_data': leg2_data,
#         'flight2_data': flight2_data,
#         'rate': rate,
#         'trip_type': trip_type,
#         'base_price': base_price,
#     }

#     return render(request, 'reservations/confirm.html', context)


def faqs(request):
    return render(request, "reservations/faqs.html")


def contact(request):
    if request.method == "POST":
        form = ContactUsFormSubmission(request.POST)
        if form.is_valid():
            form.save()
            return redirect("thankyou")
    else:
        form = ContactUsFormSubmission()
    context = {"form": form}
    return render(request, "reservations/contact.html", context)
