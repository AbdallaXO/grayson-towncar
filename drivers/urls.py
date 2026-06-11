from django.urls import path
from . import views


urlpatterns = [
    path("sw.js", views.service_worker, name="driver_service_worker"),
    path("", views.index, name="drivers_dashboard"),
    path("extend/", views.extend, name="drivers_extend"),
    path("<int:driver_id>/profile/", views.driver_profile, name="driver_profile"),
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
        "statement/<int:driver_id>/<int:payment_id>/void-line/<int:leg_payment_id>/",
        views.void_leg_payment_view,
        name="void_leg_payment",
    ),
    path(
        "statement/<int:driver_id>/<int:payment_id>/void-lines/",
        views.bulk_void_leg_payments_view,
        name="bulk_void_leg_payments",
    ),
    path(
        "statement/<int:driver_id>/<int:payment_id>/edit-line/<int:leg_payment_id>/",
        views.edit_leg_payment_amount_view,
        name="edit_leg_payment_amount",
    ),
    path(
        "statement/<int:driver_id>/<int:payment_id>/add-leg/",
        views.add_missing_leg_view,
        name="add_missing_leg_to_statement",
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
        "api/board-state/",
        views.board_state,
        name="driver_board_state",
    ),
    path(
        "api/push-subscribe/",
        views.push_subscribe,
        name="driver_push_subscribe",
    ),
    path(
        "api/push-unsubscribe/",
        views.push_unsubscribe,
        name="driver_push_unsubscribe",
    ),
    path(
        "api/push-test/",
        views.push_test,
        name="driver_push_test",
    ),
    # Early-morning wake-up checks (tokenized — no login; see views)
    path("wakeup/<str:token>/", views.wakeup_confirm, name="driver_wakeup_confirm"),
    path(
        "wakeup/<str:token>/gather/",
        views.wakeup_call_gather,
        name="driver_wakeup_gather",
    ),
    # Time-off requests (driver self-serve)
    path("time-off/", views.my_timeoff_requests, name="driver_my_timeoff_requests"),
    path("time-off/new/", views.request_timeoff, name="driver_request_timeoff"),
    path("time-off/<int:override_id>/cancel/", views.cancel_timeoff, name="driver_cancel_timeoff"),
]
