from django.urls import path
from . import views
from users.emails import send_reservation_confirmation_ajax, send_payment_reminder_ajax

urlpatterns = [
    path("", views.index, name="dashboard"),
    path(
        "legs-dashboard-export/",
        views.export_legs_dashboard_csv,
        name="legs_dashboard_export_csv",
    ),
    path(
        "reservations-list/",
        views.ReservationListView.as_view(),
        name="reservations_list",
    ),
    path("reservation/<id>", views.reservation_details, name="reservation_details"),
    path("reservation/<id>/history/", views.reservation_history, name="reservation_history"),
    path("edit-reservation/<id>", views.modify_reservation, name="modify_reservation"),
    path("legs-list/", views.legs_list, name="legs_list"),
    path("inhouse-schedule/", views.inhouse_schedule, name="inhouse_schedule"),
    path("leg/<int:id>/history/", views.leg_history, name="leg_history"),
    path("leg/<int:id>/history/partial/", views.leg_history_partial, name="leg_history_partial"),
    path("statistics/", views.statistics_page, name="statistics_page"),
    path("analytics/", views.analytics_dashboard, name="analytics_dashboard"),
    path("route-timing/", views.route_timing_reference, name="route_timing_reference"),
    path("driver-performance/", views.driver_performance, name="driver_performance"),
    path("route-timing/legs/", views.route_timing_leg_details, name="route_timing_leg_details"),
    path("route-timing/exclude-leg/", views.route_timing_exclude_leg, name="route_timing_exclude_leg"),
    path("recalculate-route-metrics/", views.recalculate_route_metrics, name="recalculate_route_metrics"),
    path("capacity-planner/", views.capacity_planner, name="capacity_planner"),
    path("auto-assign-drivers/", views.auto_assign_drivers, name="auto_assign_drivers"),
    path("find-swaps/", views.find_swap_suggestions, name="find_swap_suggestions"),
    path("execute-swap/", views.execute_swap, name="execute_swap"),
    path("execute-takeback/", views.execute_takeback, name="execute_takeback"),
    path("swap-tester/", views.swap_tester, name="swap_tester"),
    path("reset-schedule/", views.reset_schedule, name="reset_schedule"),
    path("save-snapshot/", views.save_schedule_snapshot, name="save_schedule_snapshot"),
    path("list-snapshots/", views.list_schedule_snapshots, name="list_schedule_snapshots"),
    path("restore-snapshot/", views.restore_schedule_snapshot, name="restore_schedule_snapshot"),
    path("delete-snapshot/", views.delete_schedule_snapshot, name="delete_schedule_snapshot"),
    path("smart-schedule-builder/", views.smart_schedule_builder, name="smart_schedule_builder"),
    path("update-drive-time/", views.update_drive_time, name="update_drive_time"),
    path("scheduler-settings/", views.get_scheduler_settings, name="get_scheduler_settings"),
    path("update-scheduler-settings/", views.update_scheduler_settings, name="update_scheduler_settings"),
    path("driver-weekly-schedules/", views.get_driver_weekly_schedules, name="get_driver_weekly_schedules"),
    path("save-driver-weekly-schedules/", views.save_driver_weekly_schedules, name="save_driver_weekly_schedules"),
    path("update-leg-assignment/", views.update_leg_assignment, name="update_leg_assignment"),
    path(
        "update-inhouse-vehicle-assignment/",
        views.update_inhouse_vehicle_assignment,
        name="update_inhouse_vehicle_assignment",
    ),
    path(
        "copy-vehicle-assignments/",
        views.copy_vehicle_assignments,
        name="copy_vehicle_assignments",
    ),
    path("update-contact-info/", views.update_contact_info, name="update_contact_info"),
    path("update-leg-info/", views.update_leg_info, name="update_leg_info"),
    path("refresh-flight-data/", views.refresh_flight_data, name="refresh_flight_data"),
    path("match-leg-time-to-flight/", views.match_leg_time_to_flight, name="match_leg_time_to_flight"),
    path("match-all-leg-times-to-flight/", views.match_all_leg_times_to_flight, name="match_all_leg_times_to_flight"),
    path("refresh-all-flights/", views.refresh_all_flights, name="refresh_all_flights"),
    path("confirmations/", views.confirmations_view, name="confirmations"),
    path(
        "refresh-all-flights-status/<str:task_id>/",
        views.refresh_all_flights_status,
        name="refresh_all_flights_status",
    ),
    path(
        "send_confirmation_email/",
        send_reservation_confirmation_ajax,
        name="send_confirmation_email",
    ),
    path(
        "send_payment_reminder/",
        send_payment_reminder_ajax,
        name="send_payment_reminder",
    ),
    path(
        "update_private_notes/", views.update_private_notes, name="update_private_notes"
    ),
    path(
        "process-payment/<str:reservation_id>/",
        views.create_checkout_session,
        name="process_payment",
    ),
    path("save-card/<str:reservation_id>", views.save_card, name="save_card"),
    path(
        "reservations/<uuid:reservation_id>/dispatcher-actions/",
        views.dispatcher_payment_portal,
        name="dispatcher_payment_portal",
    ),
    path(
        "update-reservation-status/",
        views.update_reservation_status,
        name="update_reservation_status",
    ),
    # Dispatcher Booking System URLs
    path(
        "booking/start/",
        views.dispatcher_booking_start,
        name="dispatcher_booking_start",
    ),
    path(
        "booking/customer/",
        views.dispatcher_booking_customer,
        name="dispatcher_booking_customer",
    ),
    path(
        "booking/reservation/",
        views.dispatcher_booking_reservation,
        name="dispatcher_booking_reservation",
    ),
    path(
        "booking/legs/",
        views.dispatcher_booking_legs,
        name="dispatcher_booking_legs",
    ),
    path(
        "booking/pricing/",
        views.dispatcher_booking_pricing,
        name="dispatcher_booking_pricing",
    ),
    path(
        "booking/review/",
        views.dispatcher_booking_review,
        name="dispatcher_booking_review",
    ),
    path(
        "booking/cancel/",
        views.dispatcher_booking_cancel,
        name="dispatcher_booking_cancel",
    ),
    # Customer Search API
    path(
        "api/customer-search/",
        views.customer_search_api,
        name="customer_search_api",
    ),
    path(
        "add-leg/",
        views.add_leg_to_reservation,
        name="add_leg_to_reservation",
    ),
    # Driver Payment Management
    path(
        "driver-payments/",
        views.driver_payment_management,
        name="driver_payment_management",
    ),
    path(
        "update-driver-pay-amount/",
        views.update_driver_pay_amount,
        name="update_driver_pay_amount",
    ),
    path(
        "process-driver-payment/",
        views.process_driver_payment,
        name="process_driver_payment",
    ),
    path(
        "driver-pay-rates/",
        views.driver_pay_rates,
        name="driver_pay_rates",
    ),
    path(
        "update-pay-rate/",
        views.update_pay_rate,
        name="update_pay_rate",
    ),
    path(
        "bulk-update-pay-rates/",
        views.bulk_update_pay_rates,
        name="bulk_update_pay_rates",
    ),
    path(
        "delete-pay-rate/",
        views.delete_pay_rate,
        name="delete_pay_rate",
    ),
    path(
        "update-inhouse-default-rate/",
        views.update_inhouse_default_rate,
        name="update_inhouse_default_rate",
    ),
    path(
        "update-night-bonus/",
        views.update_night_bonus,
        name="update_night_bonus",
    ),
    path(
        "delete-leg/",
        views.delete_leg,
        name="delete_leg",
    ),
    path(
        "delete-reservation/",
        views.delete_reservation,
        name="delete_reservation",
    ),
    # Refund Management
    path(
        "request-refund/",
        views.request_refund,
        name="request_refund",
    ),
    path(
        "refund-management/",
        views.refund_management,
        name="refund_management",
    ),
    path(
        "process-refund/",
        views.process_refund,
        name="process_refund",
    ),
]
