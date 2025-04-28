from django.urls import path
from . import views

urlpatterns = [
    path("", views.blog_list, name="blog-list"),
    path("post/<slug:slug>/", views.blog_post, name="blog-post"),
    path("category/<slug:slug>/", views.blog_category, name="blog-category"),
    path("feed/", views.BlogFeed(), name="blog-feed"),
    # Optional: Add tag-based filtering if you want to implement it later
    # path("tag/<slug:slug>/", views.blog_tag, name="blog-tag"),
]
