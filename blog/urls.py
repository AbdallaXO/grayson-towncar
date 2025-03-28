from django.urls import path
from . import views

urlpatterns = [
    path("", views.blog_list, name="blog-list"),
    path("post/<slug:slug>", views.blog_post, name="blog-post"),
]
