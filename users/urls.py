from django.urls import path
from . import views

urlpatterns = [
    path("", views.index),
    path("login/", views.loginUser, name="login"),
    path("logout/", views.logoutUser, name="logout"),
    path('register/', views.registerUser, name='register'),
    path("partner/", views.partner, name="partner")
]
