from django.urls import path
from . import views

urlpatterns = [
    path('create-checkout-session/<int:reservation_id>/', 
         views.create_checkout_session, 
         name='create_checkout_session'),
    path('cancel/', 
         views.cancel, 
         name='cancel'),
]