from django.urls import path
from . import views


urlpatterns = [
    path("", views.index, name="home"),
    path("orlando-transportation-faqs/", views.faqs, name="faqs"),
    path("about-grayson-towncar-services/", views.about_us, name="about-us"),
    path("book-orlando-transportation/<pk>", views.reservation_form, name="reserve"),
    path("contact-grayson-towncar/", views.contact, name="contact"),
]
