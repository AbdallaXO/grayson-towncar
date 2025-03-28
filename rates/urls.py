from django.urls import path
from . import views

urlpatterns = [path("", views.index, name="rates"),
               path('book/', views.BookRideView.as_view(), name="book-ride")]
