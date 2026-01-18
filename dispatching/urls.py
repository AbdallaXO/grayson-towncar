from django.urls import path
from . import views
from users.emails import send_reservation_confirmation_ajax

urlpatterns = [
    path("", views.index, name="dashboard"),
    path(
        "reservations-list/",
        views.ReservationListView.as_view(),
        name="reservations_list",
    ),
    path("reservation/<id>", views.reservation_details, name="reservation_details"),
    path("edit-reservation/<id>", views.modify_reservation, name="modify_reservation"),
    path("legs-list/", views.legs_list, name="legs_list"),
    path("statistics/", views.statistics_page, name="statistics_page"),
    path("update-leg-assignment/", views.update_leg_assignment, name="update_leg_assignment"),
    path(
        "update-inhouse-vehicle-assignment/",
        views.update_inhouse_vehicle_assignment,
        name="update_inhouse_vehicle_assignment",
    ),
    path("update-contact-info/", views.update_contact_info, name="update_contact_info"),
    path("update-leg-info/", views.update_leg_info, name="update_leg_info"),
    path("refresh-flight-data/", views.refresh_flight_data, name="refresh_flight_data"),
    path("refresh-all-flights/", views.refresh_all_flights, name="refresh_all_flights"),
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
