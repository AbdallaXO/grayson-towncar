from django.urls import path
from . import views


urlpatterns = [
    path("", views.index, name="drivers_dashboard"),
    path(
        "update_leg_status/<int:leg_id>/",
        views.update_leg_status,
        name="update_leg_status",
    ),
    path("completed-trips/", views.completed_trips, name="completed_trips"),
    path("weekly-schedule/", views.schedule, name="schedule"),
]
