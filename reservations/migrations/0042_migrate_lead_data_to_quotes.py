from django.db import migrations
from django.utils import timezone


def migrate_lead_data_to_quotes(apps, schema_editor):
    """
    Migrate existing lead data to the new Quote model structure.
    This preserves all existing leads while creating Quote records for their trip data.
    """
    Lead = apps.get_model("reservations", "Lead")
    Quote = apps.get_model("reservations", "Quote")
    Vehicle = apps.get_model("rates", "Vehicle")

    for lead in Lead.objects.all():
        # Create a quote for each lead with their trip data
        quote = Quote.objects.create(
            lead=lead,
            pickup_location=lead.pickup_location or "",
            dropoff_location=lead.dropoff_location or "",
            pickup_date=lead.pickup_date,
            trip_type=lead.trip_type or "",
            vehicle=lead.vehicle,  # This should already be a Vehicle instance
            estimated_price=lead.estimated_price,
            status="pending",
            is_current=True,
            created_at=lead.created_at,
        )

        # If the lead has notes with quote history, parse them and create additional quotes
        if lead.notes and "--- New Quote Request" in lead.notes:
            # Parse the notes to extract additional quote requests
            lines = lead.notes.split("\n")
            current_quote_data = {}

            for line in lines:
                line = line.strip()
                if line.startswith("--- New Quote Request"):
                    # Save previous quote if we have data
                    if current_quote_data:
                        # Handle vehicle lookup
                        vehicle = None
                        if current_quote_data.get("vehicle"):
                            try:
                                vehicle = Vehicle.objects.get(
                                    id=current_quote_data["vehicle"]
                                )
                            except (ValueError, Vehicle.DoesNotExist):
                                pass

                        Quote.objects.create(
                            lead=lead,
                            pickup_location=current_quote_data.get("pickup", ""),
                            dropoff_location=current_quote_data.get("dropoff", ""),
                            pickup_date=current_quote_data.get("pickup_date"),
                            trip_type=current_quote_data.get("trip_type", ""),
                            vehicle=vehicle,
                            estimated_price=current_quote_data.get("estimated_price"),
                            status="pending",
                            is_current=False,
                            created_at=current_quote_data.get(
                                "created_at", timezone.now()
                            ),
                        )
                        current_quote_data = {}
                elif ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip().lower()
                    value = value.strip()

                    if key == "pickup":
                        current_quote_data["pickup"] = value
                    elif key == "dropoff":
                        current_quote_data["dropoff"] = value
                    elif key == "vehicle":
                        try:
                            current_quote_data["vehicle"] = int(value)
                        except (ValueError, TypeError):
                            pass
                    elif key == "trip type":
                        current_quote_data["trip_type"] = (
                            "oneway" if "one way" in value.lower() else "roundtrip"
                        )
                    elif key == "estimated price":
                        try:
                            # Remove $ and commas, convert to decimal
                            price_str = value.replace("$", "").replace(",", "")
                            current_quote_data["estimated_price"] = float(price_str)
                        except (ValueError, AttributeError):
                            pass
                    elif key == "pickup date":
                        try:
                            from datetime import datetime

                            current_quote_data["pickup_date"] = datetime.strptime(
                                value, "%Y-%m-%d"
                            ).date()
                        except (ValueError, AttributeError):
                            pass

            # Save the last quote if we have data
            if current_quote_data:
                # Handle vehicle lookup
                vehicle = None
                if current_quote_data.get("vehicle"):
                    try:
                        vehicle = Vehicle.objects.get(id=current_quote_data["vehicle"])
                    except (ValueError, Vehicle.DoesNotExist):
                        pass

                Quote.objects.create(
                    lead=lead,
                    pickup_location=current_quote_data.get("pickup", ""),
                    dropoff_location=current_quote_data.get("dropoff", ""),
                    pickup_date=current_quote_data.get("pickup_date"),
                    trip_type=current_quote_data.get("trip_type", ""),
                    vehicle=vehicle,
                    estimated_price=current_quote_data.get("estimated_price"),
                    status="pending",
                    is_current=False,
                    created_at=current_quote_data.get("created_at", timezone.now()),
                )


def reverse_migrate_lead_data_to_quotes(apps, schema_editor):
    """
    Reverse migration - delete all quotes (this will lose quote history)
    """
    Quote = apps.get_model("reservations", "Quote")
    Quote.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0041_add_quote_model"),
    ]

    operations = [
        migrations.RunPython(
            migrate_lead_data_to_quotes,
            reverse_migrate_lead_data_to_quotes,
        ),
    ]
