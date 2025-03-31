from django.core.management.base import BaseCommand
from random import choice
from rates.models import Rate, Vehicle, Route, Location


class Command(BaseCommand):
    def load_vehicles(self):
        vehicle_list = [
            ["towncar", "4", "4", ""],
            ["suv", "6", "6", ""],
            ["mini_van", "5", "5", ""],
            ["van", "10", "11", ""],
        ]

        vehicles = [
            Vehicle(
                vehicle_type=item[0],
                capacity=item[1],
                luggage_capacity=item[2],
                image=item[3],
            )
            for item in vehicle_list
        ]

        self.stdout.write("> Creating Vehicles")
        for vehicle in vehicles:
            self.stdout.write(
                f">>> Creating {vehicle.vehicle_type} with capacity of {vehicle.capacity} & luggage of {vehicle.luggage_capacity}"
            )
            vehicle.save()

    def load_Locations(self):
        location_list = [
            "Orlando International Airport",
            "Universal Studios Area Hotels",
            "All WDW Disney Property Resorts",
            "International Drive Hotels",
            "Disney Springs Hotels",
            "Kissimmee 192 Area Hotels",
            "Omni Championsgate / Reunion",
            "Port Canaveral",
            "Sea World",
            "Sanford Int'l Airport",
        ]

        locations = [Location(name=item) for item in location_list]
        self.stdout.write("> Creating Locations")
        for location in locations:
            self.stdout.write(f">>> Creating {location.name}")
            location.save()

    def load_routes(self):
        locations = Location.objects.all()

        routes_list = [
            [1, 2],
            [1, 3],
            [1, 4],
            [1, 5],
            [1, 6],
            [1, 7],
            [8, 1],
            [8, 3],
            [2, 3],
            [2, 6],
            [9, 6],
            [9, 3],
            [10, 3],
            [10, 2],
        ]

        routes = [
            Route(
                origin=locations.filter(pk=item[0]).first(), 
                destination=locations.filter(pk=item[1]).first()
                ) 
            for item in routes_list
            ]
        self.stdout.write("> Creating Routes")
        for route in routes:
            self.stdout.write(f">>> Creating {str(route)}")
            route.save()

    def load_rates(self):
        vehicles = Vehicle.objects.all()
        routes = Route.objects.all()
        prices_one = [50,60,70,80,90,100]
        prices_two = [110,120,130,140,150]
        self.stdout.write("> Creating Rates")
        for vehicle in vehicles:
            for route in routes: 
                rate = Rate(
                    vehicle=vehicle,
                    route=route,
                    oneway_price= choice(prices_one),
                    round_trip_price= choice(prices_two)
                )
                self.stdout.write(
                    f">>> Creating Rate with vehicle {vehicle}, Route {route}," 
                    f"Oneway={rate.oneway_price} , Round Trip={rate.round_trip_price}"
                )
                rate.save()

    def handle(self, **options):
        self.stdout.write("> Seeding database.")
        self.load_vehicles()
        self.load_Locations()
        self.load_routes()
        self.load_rates()
        self.stdout.write("> Finished seeding database.")


