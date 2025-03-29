from django.urls import path
from . import views


urlpatterns = [
    path("", views.index, name="home"),
    path("faqs/", views.faqs, name="faqs"),
    path("book/", views.BookRideView.as_view(), name="book-ride"),
    path('about/', views.about_us, name='about-us')
]
