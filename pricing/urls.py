from django.urls import path

from . import pages, views

urlpatterns = [
    # Public marketing pages
    path("fleet/", pages.fleet_page, name="fleet"),
    path("hourly-city-to-city/", pages.charters_page, name="charters"),
    # Two-step quote: trip form (widget) -> full-width results page -> checkout
    path("transfer-quote/", views.quote_results, name="quote_results"),
    path("transfer-quote/select/", views.select_quote, name="select_quote"),
    # Quote engine
    path("api/quote/", views.quote_api, name="quote_api"),
    path("book-quote/<uuid:token>/", views.book_quote, name="book_quote"),
]
