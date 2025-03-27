from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="home"),
    path("reservation", views.make_reservation, name="make-reservation"),
]
