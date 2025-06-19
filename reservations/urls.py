from django.urls import path
from . import views


urlpatterns = [
    path("", views.index, name="home"),
    path("orlando-transportation-faqs/", views.faqs, name="faqs"),
    path("about-grayson-towncar-services/", views.about_us, name="about-us"),
    path("fleet/", views.fleet, name="fleet"),
    path("book-orlando-transportation/<pk>", views.reservation_form, name="reserve"),
    path("tos", views.tos, name="tos"),
    path("quote-form-handler/", views.quote_form_handler, name="quote_form_handler"),
]
