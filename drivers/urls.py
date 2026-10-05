from django.urls import path
from . import comms_views, onboarding_views, operator_views, regular_shift_views, views
from . import driver_knowledge_views


urlpatterns = [
    # ── Operator portal (affiliates who re-dispatch to their own drivers) ──
    path("operator/", operator_views.operator_board, name="operator_board"),
    path(
        "operator/upcoming/",
        operator_views.operator_upcoming,
        name="operator_upcoming",
    ),
    path(
        "operator/completed/",
        operator_views.operator_completed,
        name="operator_completed",
    ),
    path(
        "operator/<int:leg_id>/accept/",
        operator_views.operator_accept,
        name="operator_accept",
    ),
    path(
        "operator/<int:leg_id>/decline/",
        operator_views.operator_decline,
        name="operator_decline",
    ),
    path(
        "operator/<int:leg_id>/assign-driver/",
        operator_views.operator_assign_driver,
        name="operator_assign_driver",
    ),
    path("sw.js", views.service_worker, name="driver_service_worker"),
    path("", views.index, name="drivers_dashboard"),
    path("extend/", views.extend, name="drivers_extend"),
    path("<int:driver_id>/profile/", views.driver_profile, name="driver_profile"),
    # Regular shifts (structured shifts, Stage 1): the list, the editor, the
    # switch, and the shapes (Shift Templates)
    path("regular-shifts/", regular_shift_views.regular_shifts, name="regular_shifts"),
    path("shift-templates/", regular_shift_views.shift_templates, name="shift_templates"),
    path(
        "<int:driver_id>/regular-shift/",
        regular_shift_views.regular_shift_edit,
        name="regular_shift_edit",
    ),
    path(
        "regular-shifts/switch/",
        regular_shift_views.regular_shift_switch,
        name="regular_shift_switch",
    ),
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
    path("comms-kpis/", comms_views.comms_kpis, name="driver_comms_kpis"),
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
        "api/client-message/<int:leg_id>/",
        views.log_client_message,
        name="driver_log_client_message",
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
    # Licensing documents (driver self-serve)
    path("my-documents/", views.my_documents, name="driver_my_documents"),
    # Onboarding: add a driver, welcome links, the driver's own details
    path("new/", onboarding_views.driver_new, name="driver_new"),
    path("<int:driver_id>/invite/", onboarding_views.driver_invite, name="driver_invite"),
    path("welcome/<str:token>/", onboarding_views.driver_welcome, name="driver_welcome"),
    path("my-details/", onboarding_views.my_details, name="driver_my_details"),
    path("my-details/password/", onboarding_views.my_password, name="driver_my_password"),
    # Driver knowledge on the staff profile (staff-only, never in the driver app)
    path("<int:driver_id>/tags/add/", driver_knowledge_views.driver_tag_add,
         name="driver_tag_add"),
    path("<int:driver_id>/tags/<int:tag_id>/remove/", driver_knowledge_views.driver_tag_remove,
         name="driver_tag_remove"),
    path("<int:driver_id>/tags/new/", driver_knowledge_views.driver_tag_create,
         name="driver_tag_create"),
    path("<int:driver_id>/log/add/", driver_knowledge_views.driver_log_add,
         name="driver_log_add"),
    path("<int:driver_id>/log/<int:entry_id>/edit/", driver_knowledge_views.driver_log_edit,
         name="driver_log_edit"),
    path("<int:driver_id>/log/<int:entry_id>/delete/", driver_knowledge_views.driver_log_delete,
         name="driver_log_delete"),
]
