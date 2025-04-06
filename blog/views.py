from django.shortcuts import render, get_object_or_404
from .models import Blog


def blog_list(request):
    """A View that returns all the latest blogs sorted by most recent,
    also has gets latest post"""
    blogs = Blog.objects.all().order_by('-created')
    latest = blogs[0] if blogs else None
    return render(request, "blog/index.html", {"blogs": blogs, "latest_post": latest})


def blog_post(request, slug):
    """Gets a Single blog post by its slug from the blog_list returns 404 if none"""
    post = get_object_or_404(Blog, slug=slug)
    return render(request, "blog/blog-post.html", {"post": post})
