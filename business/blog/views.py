from django.shortcuts import render, get_object_or_404
from .models import Blog

def blog(request):
    blogs = Blog.objects.all()
    return render(request, 'blog/index.html', {'blogs':blogs})


def blog_post_detail(request, slug):
    post = get_object_or_404(Blog, slug=slug)
    return render(request, 'blog/post_detail.html', {'post': post})
