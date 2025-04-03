from django.urls import path
from . import views


urlpatterns = [
    path(
        "/checkout<reservation_id>",
        views.create_checkout_session,
        name="checkout_session",
    ),
    path("thank-you/", views.thank_you, name="thank_you"),
    path("error/", views.cancel, name="cancel"),
]
