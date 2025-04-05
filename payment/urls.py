from django.urls import path
from . import views
from .webhook import stripe_webhook

urlpatterns = [
    path(
        "create-checkout-session/<int:reservation_id>/",
        views.create_checkout_session,
        name="create_checkout_session",
    ),
    path(
        "save-card-checkout/<int:reservation_id>/",
        views.save_card,
        name="save_card_checkout",
    ),
    path("stripe/webhook/", views.stripe_webhook, name="stripe_webhook"),
]
