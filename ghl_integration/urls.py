"""
URL configuration for ghl_integration app.
"""

from django.urls import path
from . import views

urlpatterns = [
    path('webhook/', views.ghl_webhook, name='ghl_webhook'),
]
