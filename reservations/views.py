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
from .utils import _initalize_form, get_form_details, returns_post_form, validate_forms
from django.contrib import messages
from .email import send_reservation_confirmation
from reservations.templatetags.seo_tags import structured_data


# Create your views here.
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
        ) = returns_post_form(request, trip_type)

        forms_valid = validate_forms(
            customer_form,
            reservation_form,
            flight1_form,
            leg1_form,
            flight2_form,
            leg2_form,
            trip_type,
        )
        # we validate all forms, and validate leg2 form only  if the trip_type is not = one_way
        if forms_valid:
            customer = customer_form.save()
            reservation = reservation_form.save(commit=False)
            reservation.customer = customer
            reservation.trip_type = trip_type
            reservation.rate = rate
            reservation.base_price = price
            reservation.save()
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
            send_reservation_confirmation(reservation)

            return redirect("create_checkout_session", reservation_id=reservation.id)

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


def faqs(request):
    return render(request, "reservations/faqs.html")


def contact(request):
    if request.method == "POST":
        form = ContactUsFormSubmission(request.POST)
        if form.is_valid():
            form.save()
            messages.success(
                request,
                "Your quote request has been submitted! We will contact you shortly.",
            )
            return redirect("contact")
    else:
        form = ContactUsFormSubmission()
    context = {"form": form}
    return render(request, "reservations/contact.html", context)
