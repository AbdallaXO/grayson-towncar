from django.urls import path
from . import views


urlpatterns = [
    path("sw.js", views.service_worker, name="driver_service_worker"),
    path("", views.index, name="drivers_dashboard"),
    path("extend/", views.extend, name="drivers_extend"),
    path(
        "statement/<int:driver_id>/",
        views.driver_statement_list,
        name="driver_statement_list",
    ),
    path(
        "statement/<int:driver_id>/<int:payment_id>/",
        views.driver_statement_detail,
        name="driver_statement_detail",
    ),
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
    path(
        "refresh-drive-time/",
        views.refresh_drive_time,
        name="refresh_drive_time",
    ),
    path(
        "refresh-flight-data/",
        views.refresh_flight_data,
        name="driver_refresh_flight_data",
    ),
    path(
        "toggle_timing/<int:driver_id>/",
        views.toggle_timing_exclude,
        name="toggle_timing_exclude",
    ),
    path(
        "driver-eta/<int:leg_id>/",
        views.get_driver_eta,
        name="get_driver_eta",
    ),
    path(
        "report-location/",
        views.report_location,
        name="driver_report_location",
    ),
]
