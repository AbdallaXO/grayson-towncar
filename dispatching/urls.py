from django.urls import path
from . import views
from . import flight_verify_views
from users.emails import send_reservation_confirmation_ajax, send_payment_reminder_ajax
from ops import views as ops_views
from ops import leads_board as ops_leads

urlpatterns = [
    path("", views.index, name="dashboard"),

    # Leads board — date-anchored opportunities view
    path("leads-board/", ops_leads.leads_board_view, name="leads_board"),
    path("leads-board/detail/", ops_leads.lead_board_detail, name="leads_board_detail"),
    path("leads-board/offer-preview/", ops_leads.lead_board_offer_preview, name="leads_board_offer_preview"),
    path("leads-board/send-nudge/", ops_leads.lead_board_send_nudge, name="leads_board_send_nudge"),
    path("leads-board/create-task/", ops_leads.lead_board_create_task, name="leads_board_create_task"),
    path("leads-board/mark-lost/", ops_leads.lead_board_mark_lost, name="leads_board_mark_lost"),
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
    path("reservation/<id>/", views.reservation_details, name="reservation_details"),
    path("reservation/<id>/history/", views.reservation_history, name="reservation_history"),
    path("edit-reservation/<id>/", views.modify_reservation, name="modify_reservation"),
    path("legs-list/", views.legs_list, name="legs_list"),
    path("inhouse-schedule/", views.inhouse_schedule, name="inhouse_schedule"),
    path("driver-schedules/", views.driver_schedules_dashboard, name="driver_schedules_dashboard"),
    path("leg/<int:id>/history/", views.leg_history, name="leg_history"),
    path("leg/<int:id>/history/partial/", views.leg_history_partial, name="leg_history_partial"),
    path("statistics/", views.statistics_page, name="statistics_page"),
    path("analytics/", views.analytics_dashboard, name="analytics_dashboard"),
    path("accrual-revenue/", views.accrual_revenue_report, name="accrual_revenue_report"),
    path("accrual-revenue/export.csv", views.accrual_revenue_csv, name="accrual_revenue_csv"),
    path("accrual-revenue/export.txt", views.accrual_revenue_txt, name="accrual_revenue_txt"),
    path("lead-analytics/", views.lead_analytics, name="lead_analytics"),
    path("reservation-sources/", views.reservation_sources, name="reservation_sources"),
    path("reservation-sources/fix-attribution/", views.fix_booking_source_drift, name="fix_booking_source_drift"),
    path("route-timing/", views.route_timing_reference, name="route_timing_reference"),
    path("driver-performance/", views.driver_performance, name="driver_performance"),
    path("vehicle-profit/", views.vehicle_profit_report, name="vehicle_profit_report"),
    path("vehicle-profit/export.csv", views.vehicle_profit_report_csv, name="vehicle_profit_report_csv"),
    path("fleet-intel/", views.fleet_intel_dashboard, name="fleet_intel_dashboard"),
    path("fleet-intel/legs/", views.fleet_intel_leaks, name="fleet_intel_leaks"),
    path("farmout-optimizer/", views.farmout_optimizer, name="farmout_optimizer"),
    path("route-timing/legs/", views.route_timing_leg_details, name="route_timing_leg_details"),
    path("route-timing/exclude-leg/", views.route_timing_exclude_leg, name="route_timing_exclude_leg"),
    path("recalculate-route-metrics/", views.recalculate_route_metrics, name="recalculate_route_metrics"),
    path("capacity-planner/", views.capacity_planner, name="capacity_planner"),
    path("auto-assign-drivers/", views.auto_assign_drivers, name="auto_assign_drivers"),
    path("find-swaps/", views.find_swap_suggestions, name="find_swap_suggestions"),
    path("execute-swap/", views.execute_swap, name="execute_swap"),
    path("execute-takeback/", views.execute_takeback, name="execute_takeback"),
    path("swap-tester/", views.swap_tester, name="swap_tester"),
    path("schedule-board/", views.schedule_board, name="schedule_board"),
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
    path("driver-date-overrides/", views.manage_driver_date_overrides, name="manage_driver_date_overrides"),
    path("update-leg-assignment/", views.update_leg_assignment, name="update_leg_assignment"),
    path("check-feasibility/", views.check_driver_feasibility, name="check_driver_feasibility"),
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
    # Inline editor for extra stops + multi-flight on the reservation detail page
    path("leg/<int:leg_id>/stop/add/", views.add_leg_stop, name="add_leg_stop"),
    path("leg/<int:leg_id>/stop/<int:stop_id>/update/", views.update_leg_stop, name="update_leg_stop"),
    path("leg/<int:leg_id>/stop/<int:stop_id>/delete/", views.delete_leg_stop, name="delete_leg_stop"),
    path("leg/<int:leg_id>/flight/add/", views.add_leg_flight, name="add_leg_flight"),
    path("leg/<int:leg_id>/flight/<int:legflight_id>/set-controlling/", views.set_controlling_legflight, name="set_controlling_legflight"),
    path("leg/<int:leg_id>/flight/<int:legflight_id>/delete/", views.delete_leg_flight, name="delete_leg_flight"),
    path("refresh-flight-data/", views.refresh_flight_data, name="refresh_flight_data"),
    path("match-leg-time-to-flight/", views.match_leg_time_to_flight, name="match_leg_time_to_flight"),
    path("match-all-leg-times-to-flight/", views.match_all_leg_times_to_flight, name="match_all_leg_times_to_flight"),
    path("legs/<int:leg_id>/charge-afterhours-fee/", views.charge_afterhours_fee, name="charge_afterhours_fee"),
    path("charge-all-afterhours-fees/", views.charge_all_afterhours_fees, name="charge_all_afterhours_fees"),
    path("refresh-all-flights/", views.refresh_all_flights, name="refresh_all_flights"),
    path("dismiss-flight-review/", views.dismiss_flight_review, name="dismiss_flight_review"),
    path(
        "send-flight-verification-email/",
        flight_verify_views.send_flight_verification_email_ajax,
        name="send_flight_verification_email",
    ),
    path(
        "verify-flight/<str:token>/",
        flight_verify_views.flight_verification_public,
        name="flight_verification_public",
    ),
    path(
        "verify-flight/<str:token>/check/",
        flight_verify_views.flight_verification_check,
        name="flight_verification_check",
    ),
    path("confirmations/", views.confirmations_view, name="confirmations"),
    path("confirmations/save-override/", views.save_confirmation_override, name="save_confirmation_override"),
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
        "recalculate-driver-pay/",
        views.recalculate_driver_pay,
        name="recalculate_driver_pay",
    ),
    path(
        "process-driver-payment/",
        views.process_driver_payment,
        name="process_driver_payment",
    ),
    path(
        "driver-payments/gusto-export/",
        views.gusto_export_view,
        name="gusto_export",
    ),
    path(
        "bulk-update-leg-status/",
        views.bulk_update_leg_status,
        name="bulk_update_leg_status",
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
    path(
        "refund-suggestion/",
        views.refund_suggestion,
        name="refund_suggestion",
    ),
    # Affiliate Payment Dashboard
    path(
        "agency-payouts-report/",
        views.agency_payouts_report,
        name="agency_payouts_report",
    ),
    path(
        "affiliate-payments/",
        views.affiliate_payments,
        name="affiliate_payments",
    ),
    path(
        "affiliate-payments/agents/",
        views.affiliate_payments,
        {"section_lock": "agents"},
        name="affiliate_payments_agents",
    ),
    path(
        "affiliate-payments/agencies/",
        views.affiliate_payments,
        {"section_lock": "agencies"},
        name="affiliate_payments_agencies",
    ),
    path(
        "affiliate-payments/history/",
        views.affiliate_payments,
        {"section_lock": "history"},
        name="affiliate_payments_history",
    ),
    path(
        "affiliate-payments/process-agent/",
        views.process_agent_payout_view,
        name="process_agent_payout",
    ),
    path(
        "affiliate-payments/process-agency/",
        views.process_agency_payout_view,
        name="process_agency_payout",
    ),
    path(
        "affiliate-payments/preview-agent/",
        views.preview_agent_payout_view,
        name="preview_agent_payout",
    ),
    path(
        "affiliate-payments/preview-agency/",
        views.preview_agency_payout_view,
        name="preview_agency_payout",
    ),
    path(
        "affiliate-payments/process-bulk/",
        views.process_bulk_payout_view,
        name="process_bulk_payout",
    ),
    path(
        "affiliate-payments/exclude-reservation/",
        views.toggle_reservation_commission_exclusion,
        name="toggle_reservation_commission_exclusion",
    ),
    path(
        "reservations/toggle-vip/",
        views.toggle_reservation_vip,
        name="toggle_reservation_vip",
    ),
    # Travel Agent / Agency management (admin)
    path(
        "travel-agents/",
        views.admin_travel_agents,
        name="admin_travel_agents",
    ),
    path(
        "travel-agents/<int:pk>/",
        views.admin_travel_agent_detail,
        name="admin_travel_agent_detail",
    ),
    path(
        "travel-agents/<int:pk>/set-agency/",
        views.admin_travel_agent_set_agency,
        name="admin_travel_agent_set_agency",
    ),
    path(
        "travel-agents/<int:pk>/toggle-agency-pays/",
        views.admin_travel_agent_toggle_agency_pays,
        name="admin_travel_agent_toggle_agency_pays",
    ),
    path(
        "travel-agents/<int:pk>/set-rate/",
        views.admin_travel_agent_set_rate,
        name="admin_travel_agent_set_rate",
    ),
    path(
        "travel-agents/bulk-assign/",
        views.admin_travel_agents_bulk_assign,
        name="admin_travel_agents_bulk_assign",
    ),
    path(
        "travel-agencies/",
        views.admin_travel_agencies,
        name="admin_travel_agencies",
    ),
    path(
        "travel-agencies/<int:pk>/",
        views.admin_travel_agency_detail,
        name="admin_travel_agency_detail",
    ),
    # Admin Payout Detail Views
    path(
        "payout/agency/<int:payout_id>/",
        views.admin_agency_payout_detail,
        name="admin_agency_payout_detail",
    ),
    path(
        "payout/agent/<int:pk>/",
        views.admin_agent_payout_detail,
        name="admin_agent_payout_detail",
    ),
    # ── Ops Task Queue ──
    path("task-queue/", ops_views.task_queue_view, name="task_queue"),
    path("task-queue/claim/", ops_views.task_claim, name="task_claim"),
    path("task-queue/complete/", ops_views.task_complete, name="task_complete"),
    path("task-queue/snooze/", ops_views.task_snooze, name="task_snooze"),
    path("task-queue/assign/", ops_views.task_assign, name="task_assign"),
    path("task-queue/cancel/", ops_views.task_cancel, name="task_cancel"),
    path("task-queue/release/", ops_views.task_release, name="task_release"),
    path("task-queue/log-comm/", ops_views.task_log_comm, name="task_log_comm"),
    path("task-queue/create/", ops_views.task_create_manual, name="task_create_manual"),
    path("task-queue/contact-form/update-status/", ops_views.contact_form_update_status, name="contact_form_update_status"),
    path("task-queue/contact-form/delete/", ops_views.contact_form_delete, name="contact_form_delete"),
    path("task-queue/<int:task_id>/", ops_views.task_detail_view, name="task_detail"),
    # ── Staff Time Clock ──
    path("timeclock/", ops_views.timeclock_view, name="timeclock"),
    path("timeclock/action/", ops_views.timeclock_action, name="timeclock_action"),
    path("timeclock/overview/", ops_views.timeclock_overview, name="timeclock_overview"),
    path("timeclock/overview/export.csv", ops_views.timeclock_export_csv, name="timeclock_export_csv"),
    path("timeclock/manage/", ops_views.timeclock_manage, name="timeclock_manage"),
    path("timeclock/manage/<int:user_id>/", ops_views.timeclock_staff_detail, name="timeclock_staff_detail"),
    path("timeclock/manage/entry-action/", ops_views.timeclock_entry_action, name="timeclock_entry_action"),
    path("timeclock/manage/schedule/", ops_views.staff_schedule_get, name="staff_schedule_get"),
    path("timeclock/manage/schedule/action/", ops_views.staff_schedule_action, name="staff_schedule_action"),
    path("staff-metrics/", ops_views.staff_metrics_view, name="staff_metrics"),
    path("staff-kpis/", ops_views.staff_kpis_view, name="staff_kpis"),
    path("revenue-kpis/", ops_views.revenue_kpis_view, name="revenue_kpis"),
    path("staff-metrics/<int:user_id>/", ops_views.staff_detail_view, name="staff_detail"),
    # ── Admin Tasks Hub ──
    path("admin-tasks/", ops_views.admin_tasks_view, name="admin_tasks"),
    path("admin-tasks/bulk-action/", ops_views.admin_tasks_bulk_action, name="admin_tasks_bulk"),
    # Duplicate Reservation Cleanup
    path("duplicate-reservations/", views.duplicate_reservations, name="duplicate_reservations"),
    path("cancel-duplicate-reservation/", views.cancel_duplicate_reservation, name="cancel_duplicate_reservation"),
    # Quote Calculator
    path("quote-calculator/", views.quote_calculator, name="quote_calculator"),
    path("quote-calculator/calculate/", views.quote_calculator_api, name="quote_calculator_api"),
    # Driver time-off request review
    path("time-off-requests/", views.dispatcher_timeoff_requests, name="dispatcher_timeoff_requests"),
    path("time-off-requests/<int:override_id>/approve/", views.approve_timeoff_request, name="approve_timeoff_request"),
    path("time-off-requests/<int:override_id>/deny/", views.deny_timeoff_request, name="deny_timeoff_request"),
]
