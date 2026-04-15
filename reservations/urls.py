from django.urls import path
from . import views
from .views_quote_experiment import guest_quote_page


urlpatterns = [
    path("", views.index, name="home"),
    path("get-a-quote/", guest_quote_page, name="guest_quote"),
    path("orlando-transportation-faqs/", views.faqs, name="faqs"),
    path("about-grayson-towncar-services/", views.about_us, name="about-us"),
    path("book-orlando-transportation/<pk>", views.reservation_form, name="reserve"),
    path("tos/", views.tos, name="tos"),
    path("privacy/", views.privacy, name="privacy"),
    path("quote-form-handler/", views.quote_form_handler, name="quote_form_handler"),
    path("check-time-availability/", views.check_time_availability, name="check_time_availability"),
]
