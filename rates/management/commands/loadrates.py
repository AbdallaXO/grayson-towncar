from django.core.management.base import BaseCommand

from rates.models import Vehicle, Route, Rate


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

        # Vehicle.objects.bulk_create(vehicles)
        for vehicle in vehicles:
            self.stdout.write(
                f">>> Creating {vehicle.vehicle_type} with capacity of {vehicle.capacity} & luggage of {vehicle.luggage_capacity}"
            )
            vehicle.save()

    def load_routes(self):
        pass

    def handle(self, **options):
        self.stdout.write(f"> Seeding database.")
        self.load_vehicles()
        self.stdout.write(f"> Finished seeding database.")
