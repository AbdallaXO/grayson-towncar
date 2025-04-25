from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="dispatcher_dashboard"),
    path("reservation/<id>", views.reservation_details, name="reservation_details"),
    
]
