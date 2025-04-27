from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="dispatcher_dashboard"),
    path("reservation/<id>", views.reservation_details, name="reservation_details"),
    path("edit-reservation/<id>", views.modify_reservation, name="edit_reservation"),
    
]
