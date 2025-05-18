from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="rates"),
    path('quote/', views.quote, name='quote'),
    path('save-lead/', views.save_lead, name='save_lead'),
]
