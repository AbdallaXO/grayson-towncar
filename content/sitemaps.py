from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from blog.models import Blog


class StaticPagesSitemap(Sitemap):
    protocol = "https"

    PRIORITY_MAP = {
        "home": (1.0, "weekly"),
        "rates": (0.9, "monthly"),
        "services": (0.8, "monthly"),
        "blog-list": (0.7, "weekly"),
        "faqs": (0.7, "monthly"),
        "about-us": (0.7, "monthly"),
        "contact": (0.6, "monthly"),
        "partner": (0.5, "monthly"),
    }

    def items(self):
        return list(self.PRIORITY_MAP.keys())

    def location(self, item):
        return reverse(item)

    def priority(self, item):
        return self.PRIORITY_MAP[item][0]

    def changefreq(self, item):
        return self.PRIORITY_MAP[item][1]


class ServicePagesSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.9
    protocol = "https"

    def items(self):
        return [
            "orlando_airport_transportation",
            "disney_world_transportation",
            "universal_orlando_transportation",
            "epic_universe_transportation",
            "port_canaveral_transportation",
            "car_seats_transportation",
            "mco_terminal_c_transportation",
            "corporate_transportation",
        ]

    def location(self, item):
        return reverse(item)


class BlogPostSitemap(Sitemap):
    changefreq = "yearly"
    priority = 0.6
    protocol = "https"

    def items(self):
        return Blog.objects.order_by("-created")

    def lastmod(self, obj):
        return obj.created

    def location(self, obj):
        return reverse("blog-post", kwargs={"slug": obj.slug})
