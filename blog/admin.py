from django.contrib import admin
from .models import Blog
from django.utils.html import strip_tags
import html

class BlogAdmin(admin.ModelAdmin):
    list_display = ('title', 'created', 'user', 'get_short_content')
    list_filter = ('created', 'user')
    search_fields = ('title', 'content')
    prepopulated_fields = {'slug': ('title',)}
    readonly_fields = ('created',)
    fieldsets = (
        ('Basic Information', {
            'fields': ('title', 'slug', 'user', 'image')
        }),
        ('Content', {
            'fields': ('content',)
        }),
        ('Metadata', {
            'fields': ('created',),
            'classes': ('collapse',)
        }),
    )
    
    def get_short_content(self, obj):
        text = strip_tags(obj.content)
        text = html.unescape(text)
        return text[:100] + '...' if len(text) > 100 else text
    get_short_content.short_description = 'Preview'

admin.site.register(Blog, BlogAdmin)