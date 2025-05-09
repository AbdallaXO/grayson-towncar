from django.urls import path
from . import views

urlpatterns = [
    path("", views.partner, name="partner"),
    path("login/", views.loginUser, name="login"),
    path("logout/", views.logoutUser, name="logout"),
    path("register/", views.registerUser, name="register"),
    path("thank-you/", views.thankYou, name="thankyou"),
    path("contact-grayson-towncar/", views.contact, name="contact"),
    path('newsletter/subscribe/', views.newsletter_subscribe, name='newsletter_subscribe'),
]
