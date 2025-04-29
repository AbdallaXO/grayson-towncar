from django.urls import path
from . import views


urlpatterns = [path("", views.index, name="drivers_dashboard"),
               path("all-legs/", views.all_legs, name="all_legs"),
               path('weekly-schedule/', views.week_schedule, name="weekly_schedule"),]
