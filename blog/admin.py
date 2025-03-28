from django.contrib import admin
from .models import Blog


class BlogPostAdmin(admin.ModelAdmin):
    prepopulated_fields = {
        "slug": ("title",)
    }  # Automatically populate the slug based on the title


admin.site.register(Blog, BlogPostAdmin)
