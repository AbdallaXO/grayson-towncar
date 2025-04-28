from django.shortcuts import render, get_object_or_404
from django.contrib.syndication.views import Feed
from django.utils.html import strip_tags
from django.core.paginator import Paginator
from django.urls import reverse
from .models import Blog


def blog_list(request):
    """
    View for displaying the list of blog posts with pagination.
    Includes a featured latest post and handles search functionality.
    """
    # Get all blogs ordered by creation date (newest first)
    blogs_queryset = Blog.objects.all().order_by("-created")

    # Handle search functionality
    search_query = request.GET.get("search", "")
    if search_query:
        blogs_queryset = blogs_queryset.filter(
            title__icontains=search_query
        ) | blogs_queryset.filter(content__icontains=search_query)

    # Get the latest post for featured section
    latest_post = blogs_queryset.first() if not search_query else None
    # Paginate remaining posts (excluding latest if not in search mode)
    remaining_posts = blogs_queryset[1:] if latest_post else blogs_queryset
    paginator = Paginator(remaining_posts, 9)  # Show 9 posts per page
    page = request.GET.get("page")
    blogs_paginated = paginator.get_page(page)

    context = {
        "latest_post": latest_post,
        "blogs": blogs_paginated,
        "search_query": search_query,
        "page_title": "Orlando Travel Tips & Transportation Guides | Grayson Towncar Blog",
        "page_description": "Expert travel tips and guides for Orlando visitors. Learn about transportation options, theme park planning, and making the most of your Orlando vacation.",
    }
    return render(request, "blog/blog_list.html", context)


def blog_post(request, slug):
    """
    View for displaying a single blog post with related posts.
    Includes estimated reading time calculation.
    """
    post = get_object_or_404(Blog, slug=slug)
    # Get related posts (can be customized based on your requirements)
    related_posts = Blog.objects.exclude(id=post.id).order_by("-created")[:3]

    # Estimate read time
    words_per_minute = 200
    content_text = strip_tags(post.content)
    word_count = len(content_text.split())
    estimated_read_time = max(1, round(word_count / words_per_minute))

    context = {
        "post": post,
        "related_posts": related_posts,
        "estimated_read_time": estimated_read_time,
    }
    return render(request, "blog/blog_post.html", context)


def blog_category(request, slug):
    """
    View for displaying blog posts filtered by category.
    Note: This requires adding a Category model or adapting to your specific needs.
    """
    # This is a placeholder. Since your current model doesn't have categories,
    # you'll need to implement this after adding a Category model.
    # For now, redirect to the main blog list
    return blog_list(request)


class BlogFeed(Feed):
    """
    RSS feed for the blog posts.
    """

    title = "Grayson Towncar Blog"
    link = "/blog/"
    description = "Expert travel tips and guides for Orlando visitors."

    def items(self):
        return Blog.objects.order_by("-created")[:10]

    def item_title(self, item):
        return item.title

    def item_description(self, item):
        return item.get_clean_preview(words=50)

    def item_link(self, item):
        return reverse("blog-post", args=[item.slug])

    def item_pubdate(self, item):
        return item.created
