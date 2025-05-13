from django.urls import path
from . import views
from users.emails import send_reservation_confirmation_ajax

urlpatterns = [
    path("", views.index, name="dashboard"),
    path("reservations-list/", views.all_reservations, name="reservations_list"),
    path("reservation/<id>", views.reservation_details, name="reservation_details"),
    path("edit-reservation/<id>", views.modify_reservation, name="modify_reservation"),
    path("legs-list/", views.legs_list, name="legs_list"),
    path(
        "update_leg_assignment/",
        views.update_leg_assignment,
        name="update_leg_assignment",
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
        path('reservations/<uuid:reservation_id>/dispatcher-actions/', views.dispatcher_payment_portal, name='dispatcher_payment_portal'),

]
