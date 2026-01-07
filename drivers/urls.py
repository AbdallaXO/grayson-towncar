from django.urls import path
from . import views


urlpatterns = [
    path("", views.index, name="drivers_dashboard"),
    path("extend/", views.extend, name="drivers_extend"),
    path(
        "update_leg_status/<int:leg_id>/",
        views.update_leg_status,
        name="update_leg_status",
    ),
    path(
        "accept_job/<int:leg_id>/",
        views.accept_job,
        name="accept_job",
    ),
    path("completed-trips/", views.completed_trips, name="completed_trips"),
    path("weekly-schedule/", views.schedule, name="schedule"),
    path(
        "update_driver_notes/<int:leg_id>/",
        views.update_driver_notes,
        name="update_driver_notes",
    ),
    path(
        "update_notes/<int:driver_id>/",
        views.update_driver_notes_ajax,
        name="update_driver_notes_ajax",
    ),
]
