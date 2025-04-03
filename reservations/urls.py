from django.urls import path
from . import views


urlpatterns = [
    path("", views.index, name="home"),
    path("faqs/", views.faqs, name="faqs"),
    path("about/", views.about_us, name="about-us"),
    path("reserve/<pk>", views.reservation_form, name="reserve"),
    path("contact/", views.contact, name="contact")
]
