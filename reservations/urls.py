from django.urls import path
from . import views


urlpatterns = [
    path("", views.index, name="home"),
    path("faqs/", views.faqs, name="faqs"),
    path('about/', views.about_us, name='about-us'),
]
