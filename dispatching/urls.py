from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="dashboard"),
    path("reservations-list/", views.all_reservations, name="reservations_list"),
    path("reservation/<id>", views.reservation_details, name="reservation_details"),
    path("edit-reservation/<id>", views.modify_reservation, name="modify_reservation"),
    path("legs-list/", views.legs_list, name="legs_list"),
    path('update_leg_assignment/', views.update_leg_assignment, name='update_leg_assignment'),
]
