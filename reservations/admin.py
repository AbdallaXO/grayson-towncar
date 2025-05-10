# reservations/admin.py
from datetime import timedelta

from django.contrib import admin
from django import forms
from django.utils.html import format_html
from django.utils import timezone
from django.db.models import Min

from import_export import resources, fields
from import_export.admin import ImportExportModelAdmin

from .models import Customer, Reservation, Leg, Flight


# ─── Import / Export resources ──────────────────────────────────────────
class CustomerResource(resources.ModelResource):
    class Meta:
        model = Customer
        fields = (
            "id",
            "first_name",
            "last_name",
            "email",
            "phone_number",
            "is_returning",
        )
        export_order = fields


class ReservationResource(resources.ModelResource):
    customer_full_name = fields.Field(column_name="customer_full_name", readonly=True)

    def dehydrate_customer_full_name(self, obj):
        c = obj.customer
        return f"{c.first_name} {c.last_name}" if c else ""

    class Meta:
        model = Reservation
        fields = (
            "id",
            "customer_full_name",
            "trip_type",
            "vehicle",
            "total_price",
            "additional_charges",
            "status",
        )
        export_order = fields


class LegResource(resources.ModelResource):
    customer_name = fields.Field(column_name="customer_name", readonly=True)

    def dehydrate_customer_name(self, obj):
        c = obj.reservation.customer if obj.reservation else None
        return f"{c.first_name} {c.last_name}" if c else ""

    class Meta:
        model = Leg
        fields = (
            "id",
            "customer_name",
            "pickup_date",
            "pickup_time",
            "pickup_location",
            "dropoff_location",
        )
        export_order = fields


# ─── Forms ──────────────────────────────────────────────────────────────
class LegAdminForm(forms.ModelForm):
    class Meta:
        model = Leg
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "pickup_time" in self.fields:
            f = self.fields["pickup_time"]
            f.widget.format = "%I:%M %p"
            f.input_formats = ["%I:%M %p", "%H:%M"]


# ─── Inlines ────────────────────────────────────────────────────────────
class LegInline(admin.StackedInline):
    model = Leg
    form = LegAdminForm
    extra = 1
    show_change_link = True
    fieldsets = (
        (
            "Pick-up Details",
            {
                "fields": (
                    "pickup_date",
                    "pickup_time",
                    "pickup_location",
                    "flight_information",
                )
            },
        ),
        ("Drop-off", {"fields": ("dropoff_location",)}),
        (
            "Driver & Status",
            {
                "fields": (
                    "driver",
                    "status",
                )
            },
        ),
        ("Notes", {"fields": ("private_notes",), "classes": ("collapse",)}),
    )
    classes = ("wide",)
    readonly_fields = ("status",)

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        formset.form.base_fields["driver"].widget.attrs["style"] = "width: 100%;"
        return formset


# ─── Custom list filter for pick-up ranges ──────────────────────────────
class FirstPickupDateFilter(admin.SimpleListFilter):
    title = "pickup date"
    parameter_name = "first_pickup"

    def lookups(self, request, model_admin):
        return (
            ("today", "Today"),
            ("tomorrow", "Tomorrow"),
            ("next7", "Next 7 days"),
            ("past", "Past"),
        )

    def queryset(self, request, qs):
        today = timezone.localdate()
        if self.value() == "today":
            return qs.filter(earliest_leg_date=today)
        if self.value() == "tomorrow":
            return qs.filter(earliest_leg_date=today + timedelta(days=1))
        if self.value() == "next7":
            return qs.filter(
                earliest_leg_date__range=(today, today + timedelta(days=7))
            )
        if self.value() == "past":
            return qs.filter(earliest_leg_date__lt=today)
        return qs


# ─── Admin classes ──────────────────────────────────────────────────────
@admin.register(Customer)
class CustomerAdmin(ImportExportModelAdmin):
    resource_class = CustomerResource
    list_display = (
        "first_name",
        "last_name",
        "email",
        "phone_number",
        "reservation_count",
        "is_returning",
        "created_at",
    )
    list_editable = ("phone_number", "is_returning")
    list_filter = ("is_returning", "created_at")
    search_fields = ("first_name", "last_name", "email", "phone_number")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 50


@admin.register(Reservation)
class ReservationAdmin(ImportExportModelAdmin):
    resource_class = ReservationResource
    inlines = [LegInline]
    readonly_fields = ("created_at", "updated_at", "payment_status_display")

    # ── queryset with annotations
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.annotate(
            earliest_leg_date=Min("legs__pickup_date"),
            earliest_leg_time=Min("legs__pickup_time"),
        )
        return qs.order_by("earliest_leg_date", "earliest_leg_time", "id")

    # ── list view
    list_display = (
        "id",
        "customer_link",
        "legs_display",  # New method to display all legs
        "trip_type",
        "payment_status_display",
        "total_price_display",
        "vehicle",
        "created_at",
        "status",
    )
    list_display_links = ("id", "customer_link")
    list_editable = ("status",)
    list_filter = (FirstPickupDateFilter, "trip_type", "status")
    search_fields = (
        "customer__first_name",
        "customer__last_name",
        "id",
        "vehicle__vehicle_type",
    )
    list_per_page = 50

    fieldsets = (
        (
            "Reservation",
            {
                "fields": (
                    "customer",
                    "vehicle",
                    "passenger_count",
                    "luggage_count",
                    "need_carseats",
                    "store_stop",
                    ("ff_carseats", "rf_carseats", "booster_seats"),
                    "special_requests",
                    "private_notes",
                    ("total_price", "additional_charges"),
                    "base_price",
                    "trip_type",
                    "status",
                    "payment_status_display",
                    "commission_amount",
                    "travel_agent",
                    "commission_paid",
                ),
            },
        ),
    )

    # ── helpers ────────────────────────────────────────────
    @admin.display(description="Pick-ups")
    def legs_display(self, obj):
        """Display all legs with their dates and times combined in a single field with clickable links"""
        legs = obj.legs.all().order_by("pickup_date", "pickup_time")
        if not legs:
            return "-"

        result = []
        for i, leg in enumerate(legs):
            # Format date as "Saturday, May 9"
            formatted_date = (
                leg.pickup_date.strftime("%A, %B %d") if leg.pickup_date else "-"
            )
            pickup_time_str = (
                leg.pickup_time.strftime("%I:%M %p") if leg.pickup_time else "-"
            )

            # Create a link to the leg admin page
            leg_url = f"/admin/reservations/leg/{leg.id}/change/"

            # Format the link with HTML
            result.append(
                format_html(
                    '<a href="{}">{}: {} {} - {} to {}</a>',
                    leg_url,
                    f"Leg {i + 1}",
                    formatted_date,
                    pickup_time_str,
                    leg.pickup_location,
                    leg.dropoff_location,
                )
            )

        return format_html("<br>".join(result))

    @admin.display(description="Customer")
    def customer_link(self, obj):
        if not obj.customer:
            return "-"
        url = f"/admin/reservations/customer/{obj.customer.id}/change/"
        return format_html(
            '<a href="{}">{} {}</a>',
            url,
            obj.customer.first_name,
            obj.customer.last_name,
        )

    @admin.display(description="Price")
    def total_price_display(self, obj):
        return f"${obj.total_price:.2f}"

    @admin.display(description="Payment Status")
    def payment_status_display(self, obj):
        # Check if payments related manager exists and has items
        if not hasattr(obj, "payments") or not obj.payments.exists():
            return "-"

        # Get the last payment
        payment = obj.payments.last()

        # Generate the correct URL to the Payment admin page
        payment_url = f"/admin/payment/payment/{payment.id}/change/"

        # Define status to color mapping
        status_color = {
            "paid": "green",
            "pending": "orange",
            "failed": "red",
            "card_saved": "blue",  # New status for 'card saved'
        }

        # Get color based on payment status
        colour = status_color.get(
            payment.status, "gray"
        )  # Default to gray for unknown statuses

        # Return a formatted display with a clickable link to the Payment page
        return format_html(
            '<a href="{}" style="color:{};font-weight:bold;">{}</a><br/>Amount: ${}<br/>Type: {}',
            payment_url,  # Link to the Payment admin page
            colour,
            payment.status.capitalize(),
            payment.amount,
            payment.payment_type.replace("_", " ").title(),
        )


@admin.register(Leg)
class LegAdmin(ImportExportModelAdmin):
    resource_class = LegResource
    form = LegAdminForm
    list_display = (
        "pickup_date",
        "pickup_time",
        "reservation_link",  # Use a custom method for the reservation link
        "vehicle",
        "pickup_location",
        "dropoff_location",
        "driver",
        "get_status",
    )
    list_filter = ("pickup_date", "reservation__status")
    search_fields = (
        "pickup_location",
        "dropoff_location",
        "reservation__customer__first_name",
        "reservation__customer__last_name",
    )
    ordering = ("pickup_date", "pickup_time")
    list_editable = ("driver",)
    list_per_page = 50
    autocomplete_fields = ("reservation",)

    @admin.display(description="Reservation")
    def reservation_link(self, obj):
        if obj.reservation:
            url = f"/admin/reservations/reservation/{obj.reservation.id}/change/"
            return format_html('<a href="{}">{}</a>', url, obj.reservation)
        return "-"

    @admin.display(description="Status")
    def get_status(self, obj):
        return obj.reservation.status if obj.reservation else "-"

    @admin.display(description="Reservation Vehicle")
    def vehicle(self, obj):
        if obj.reservation and obj.reservation.vehicle:
            return (
                obj.reservation.vehicle.get_vehicle_type_display()
            )  # Adjust based on your Vehicle model
        return "-"


@admin.register(Flight)
class FlightAdmin(admin.ModelAdmin):
    list_display = ("airline", "flight_number", "flight_type")
    list_filter = ("flight_type", "airline")
    search_fields = ("airline", "flight_number")
    ordering = ("airline", "flight_number")
