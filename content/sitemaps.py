from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class StaticPagesSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.8
    protocol = "https"

    def items(self):
        return [
            "home",
            "rates",
            "contact",
            "about-us",
            "faqs",
            "partner",
            "services",
            "blog-list",
        ]

    def location(self, item):
        return reverse(item)

    def priority(self, item):
        if item == "home":
            return 1.0
        if item == "rates":
            return 0.9
        return 0.7


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
