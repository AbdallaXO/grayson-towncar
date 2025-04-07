from django.urls import path
from . import views
from . import webhook

urlpatterns = [
    path(
        f"checkout-session/<str:reservation_id>/",
        views.create_checkout_session,
        name="create_checkout_session",
    ),
    path(
        "save-card-checkout/<str:reservation_id>/",
        views.save_card,
        name="save_card_checkout",
    ),
    path("webhooks/stripe/", webhook.stripe_webhook, name="stripe_webhook"),
    path("payment/success/", views.payment_success, name="payment_success"),
    path("payment/cancel/", views.payment_cancel, name="payment_cancel"),
]
