from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="services"),
    path(
        "orlando-airport-transportation/",
        views.orlando_airport_transportation,
        name="orlando_airport_transportation",
    ),
    path(
        "disney-world-transportation/",
        views.disney_world_transportation,
        name="disney_world_transportation",
    ),
    path(
        "universal-orlando-transportation/",
        views.universal_orlando_transportation,
        name="universal_orlando_transportation",
    ),
    path(
        "port-canaveral-transportation/",
        views.port_canaveral_transportation,
        name="port_canaveral_transportation",
    ),
    path(
        "corporate-transportation/",
        views.corporate_transportation,
        name="corporate_transportation",
    ),
    path(
        "epic-universe-transportation/",
        views.epic_universe_transportation,
        name="epic_universe_transportation",
    ),
    path(
        "mco-terminal-c-transportation/",
        views.mco_terminal_c_transportation,
        name="mco_terminal_c_transportation",
    ),
    path(
        "car-seats/",
        views.car_seats_transportation,
        name="car_seats_transportation",
    ),
]
