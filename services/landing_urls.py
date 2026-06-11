"""Top-level SEO landing pages (mounted at the site root, not under /services/)."""

from django.urls import path
from . import views

urlpatterns = [
    path("mco-to-disney-world/", views.mco_to_disney_world, name="mco_to_disney_world"),
    path(
        "mears-alternative-orlando/",
        views.mears_alternative_orlando,
        name="mears_alternative_orlando",
    ),
    path(
        "sanford-airport-transportation/",
        views.sanford_airport_transportation,
        name="sanford_airport_transportation",
    ),
    path(
        "orlando-car-service-international-drive/",
        views.orlando_car_service_international_drive,
        name="orlando_car_service_international_drive",
    ),
    path(
        "orlando-car-service-kissimmee/",
        views.orlando_car_service_kissimmee,
        name="orlando_car_service_kissimmee",
    ),
    path(
        "car-service-lake-buena-vista/",
        views.car_service_lake_buena_vista,
        name="car_service_lake_buena_vista",
    ),
    path(
        "car-service-championsgate-reunion/",
        views.car_service_championsgate_reunion,
        name="car_service_championsgate_reunion",
    ),
]
