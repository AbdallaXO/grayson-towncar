from django.db import models
from django.contrib.auth.models import User
from django.utils.text import slugify
from ckeditor.fields import RichTextField
from django.utils.html import strip_tags
import html


class Blog(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, blank=True, null=True)
    title = models.CharField(max_length=100)
    created = models.DateTimeField(auto_now_add=True)
    slug = models.SlugField(unique=True, blank=True, null=True, db_index=True)
    image = models.ImageField(upload_to="blog/")
    content = RichTextField()

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    def get_clean_preview(self, words=20):
        """
        Returns a clean preview of the blog content without HTML tags or entities
        """
        if not self.content:
            return ""
        # Strip HTML tags
        text = strip_tags(self.content)
        # Convert HTML entities to their corresponding characters
        text = html.unescape(text)
        # Remove extra whitespace and truncate
        words_list = text.split()
        return " ".join(words_list[:words]) + ("..." if len(words_list) > words else "")
