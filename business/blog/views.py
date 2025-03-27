from django.shortcuts import render, get_object_or_404
from .models import Blog

def blog(request):
    latest = Blog.objects.latest('created')
    blogs = Blog.objects.all().order_by('created')
    return render(request, 'blog/index.html', {'blogs':blogs, 'latest_post':latest})


def blog_post(request, slug):
    post = get_object_or_404(Blog, slug=slug)
    return render(request, 'blog/blog-post.html', {'post': post})

