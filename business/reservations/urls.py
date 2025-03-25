from django.urls import path
from  . import views

urlpatterns = [
    path('', views.index),
    path('reservation', views.make_reservation, name='make-reservation')
]