# reservations/admin.py
from datetime import timedelta

from django.contrib import admin
from django import forms
from django.utils.html import format_html
from django.utils import timezone
from django.db.models import Min, Count, Q
from django.urls import reverse
from django.contrib.admin import SimpleListFilter
from rates.models import Vehicle
from import_export import resources, fields
from import_export.admin import ImportExportModelAdmin

from .models import Customer, Reservation, Leg, Flight, Lead, Quote


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
    leg_count = fields.Field(column_name="leg_count", readonly=True)
    earliest_pickup = fields.Field(column_name="earliest_pickup", readonly=True)

    def dehydrate_customer_full_name(self, obj):
        c = obj.customer
        return f"{c.first_name} {c.last_name}" if c else ""

    def dehydrate_leg_count(self, obj):
        return obj.legs.count()

    def dehydrate_earliest_pickup(self, obj):
        try:
            first_leg = obj.legs.all().order_by("pickup_date", "pickup_time").first()
            if first_leg and first_leg.pickup_date:
                return f"{first_leg.pickup_date.strftime('%Y-%m-%d')} {first_leg.pickup_time.strftime('%H:%M') if first_leg.pickup_time else ''}"
        except:
            pass
        return ""

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
            "leg_count",
            "earliest_pickup",
            "created_at",
        )
        export_order = fields


class LegResource(resources.ModelResource):
    customer_name = fields.Field(column_name="customer_name", readonly=True)
    reservation_id = fields.Field(column_name="reservation_id", readonly=True)
    vehicle = fields.Field(column_name="vehicle", readonly=True)

    def dehydrate_customer_name(self, obj):
        c = obj.reservation.customer if obj.reservation else None
        return f"{c.first_name} {c.last_name}" if c else ""

    def dehydrate_reservation_id(self, obj):
        return obj.reservation.id if obj.reservation else ""

    def dehydrate_vehicle(self, obj):
        return (
            str(obj.reservation.vehicle)
            if obj.reservation and obj.reservation.vehicle
            else ""
        )

    class Meta:
        model = Leg
        fields = (
            "id",
            "reservation_id",
            "customer_name",
            "pickup_date",
            "pickup_time",
            "pickup_location",
            "dropoff_location",
            "vehicle",
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
        (
            "Notes",
            {"fields": ("private_notes", "driver_notes"), "classes": ("collapse",)},
        ),
    )
    classes = ("wide",)
    readonly_fields = ("status",)

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        formset.form.base_fields["driver"].widget.attrs["style"] = "width: 100%;"
        return formset


# ─── Custom list filters ─────────────────────────────────────────────────
class FirstPickupDateFilter(SimpleListFilter):
    title = "pickup date"
    parameter_name = "first_pickup"

    def lookups(self, request, model_admin):
        return (
            ("today", "Today"),
            ("tomorrow", "Tomorrow"),
            ("next3", "Next 3 days"),
            ("next7", "Next 7 days"),
            ("next30", "Next 30 days"),
            ("past", "Past pickups"),
            ("no_pickup", "No pickup date"),
        )

    def queryset(self, request, qs):
        today = timezone.localdate()
        if self.value() == "today":
            return qs.filter(earliest_leg_date=today)
        if self.value() == "tomorrow":
            return qs.filter(earliest_leg_date=today + timedelta(days=1))
        if self.value() == "next3":
            return qs.filter(
                earliest_leg_date__range=(today, today + timedelta(days=3))
            )
        if self.value() == "next7":
            return qs.filter(
                earliest_leg_date__range=(today, today + timedelta(days=7))
            )
        if self.value() == "next30":
            return qs.filter(
                earliest_leg_date__range=(today, today + timedelta(days=30))
            )
        if self.value() == "past":
            return qs.filter(earliest_leg_date__lt=today)
        if self.value() == "no_pickup":
            return qs.filter(earliest_leg_date__isnull=True)
        return qs


class DriverAssignmentFilter(SimpleListFilter):
    title = "driver assignment"
    parameter_name = "driver_status"

    def lookups(self, request, model_admin):
        return (
            ("assigned", "Has driver"),
            ("unassigned", "Needs driver"),
        )

    def queryset(self, request, qs):
        if self.value() == "assigned":
            return qs.filter(driver__isnull=False)
        if self.value() == "unassigned":
            return qs.filter(driver__isnull=True)
        return qs


class CommissionStatusFilter(SimpleListFilter):
    title = "commission status"
    parameter_name = "commission_status"

    def lookups(self, request, model_admin):
        return (
            ("with_agent", "Has travel agent"),
            ("commission_paid", "Commission paid"),
            ("commission_unpaid", "Commission unpaid"),
        )

    def queryset(self, request, qs):
        if self.value() == "with_agent":
            return qs.filter(travel_agent__isnull=False)
        if self.value() == "commission_paid":
            return qs.filter(commission_paid=True)
        if self.value() == "commission_unpaid":
            return qs.filter(travel_agent__isnull=False, commission_paid=False)
        return qs


class MultipleQuotesFilter(SimpleListFilter):
    title = "quote requests"
    parameter_name = "quote_requests"

    def lookups(self, request, model_admin):
        return (
            ("single", "Single Quote"),
            ("multiple", "Multiple Quotes"),
        )

    def queryset(self, request, queryset):
        if self.value() == "single":
            return queryset.annotate(quote_count=Count("quotes")).filter(quote_count=1)
        elif self.value() == "multiple":
            return queryset.annotate(quote_count=Count("quotes")).filter(
                quote_count__gt=1
            )


# ─── Admin classes ──────────────────────────────────────────────────────
@admin.register(Customer)
class CustomerAdmin(ImportExportModelAdmin):
    resource_class = CustomerResource
    list_display = (
        "first_name",
        "last_name",
        "email",
        "phone_number",
        "reservation_link_count",
        "is_returning",
        "created_at",
    )
    list_editable = ("phone_number", "is_returning")
    list_filter = ("is_returning", "created_at")
    search_fields = ("first_name", "last_name", "email", "phone_number")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 50

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # Use a different name for the annotation to avoid conflicts
        return qs.annotate(total_reservations=Count("reservation"))

    @admin.display(description="Reservations")
    def reservation_link_count(self, obj):
        # Use the annotation or fallback to the model field
        count = getattr(obj, "total_reservations", obj.reservation_count)
        if count:
            url = (
                reverse("admin:reservations_reservation_changelist")
                + f"?customer={obj.id}"
            )
            return format_html('<a href="{}">{}</a>', url, count)
        return "0"

    actions = ["mark_as_returning", "export_customer_list"]

    @admin.action(description="Mark selected customers as returning")
    def mark_as_returning(self, request, queryset):
        updated = queryset.update(is_returning=True)
        self.message_user(request, f"{updated} customers marked as returning.")

    @admin.action(description="Export customer list with details")
    def export_customer_list(self, request, queryset):
        # Implementation would use the export functionality
        pass


@admin.register(Reservation)
class ReservationAdmin(ImportExportModelAdmin):
    ordering = ("-id",)
    resource_class = ReservationResource
    inlines = [LegInline]
    readonly_fields = (
        "created_at",
        "updated_at",
        "payment_status_display",
        "uuid",
        "profit_percentage",
    )

    # ── queryset with annotations and optimizations
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = (
            qs.select_related(
                "customer", "vehicle", "travel_agent", "travel_agent__user"
            )
            .prefetch_related(
                "legs", "legs__driver", "legs__flight_information", "payments"
            )
            .annotate(
                earliest_leg_date=Min("legs__pickup_date"),
                earliest_leg_time=Min("legs__pickup_time"),
                leg_count=Count("legs"),
            )
        )
        return qs.order_by("earliest_leg_date", "earliest_leg_time", "id")

    # ── list view
    list_display = (
        "id",
        "customer_link",
        "legs_display",
        "trip_type",
        "payment_status_display",
        "vehicle_display",
        "total_price_display",
        "profit_display",
        "agent_info",
        "status",
        "utm_source_display",
        "utm_campaign_display",
    )
    list_display_links = ("id", "customer_link")
    list_editable = ("status",)
    list_filter = (
        FirstPickupDateFilter,
        "trip_type",
        "status",
        CommissionStatusFilter,
        ("travel_agent", admin.RelatedOnlyFieldListFilter),
    )
    search_fields = (
        "customer__first_name",
        "customer__last_name",
        "id",
        "uuid",
        "vehicle__vehicle_type",
        "legs__pickup_location",
        "legs__dropoff_location",
    )
    list_per_page = 50

    # Custom actions for bulk operations
    actions = [
        "mark_as_confirmed",
        "mark_as_completed",
        "mark_as_cancelled",
        "update_profit_calculations",
    ]

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
                    ("total_price", "base_price", "additional_charges"),
                    ("total_driver_payments", "profit_estimate", "profit_percentage"),
                    "rate",
                    "trip_type",
                    "status",
                    "payment_status_display",
                )
            },
        ),
        (
            "Marketing Attribution",
            {
                "fields": (
                    "utm_source",
                    "utm_campaign",
                    "gclid",
                    "utm_medium",
                    "utm_term",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Commission Information",
            {
                "fields": (
                    "travel_agent",
                    "commission_amount",
                    "commission_paid",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "System Information",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                    "uuid",
                ),
                "classes": ("collapse",),
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
                leg.pickup_date.strftime("%a, %b %d") if leg.pickup_date else "-"
            )
            pickup_time_str = (
                leg.pickup_time.strftime("%I:%M %p") if leg.pickup_time else "-"
            )

            # Driver info
            driver_info = f" (Driver: {leg.driver})" if leg.driver else " (No driver)"

            # Status indicator
            status_icon = "✓" if leg.status == "completed" else "•"

            # Create a link to the leg admin page
            leg_url = reverse("admin:reservations_leg_change", args=[leg.id])

            # Format the link with HTML
            result.append(
                format_html(
                    '<a href="{}" title="{}"><span style="color: {};">{}</span> {} {} - {} to {}{}</a>',
                    leg_url,
                    f"Edit Leg #{leg.id}",
                    "green" if leg.status == "completed" else "black",
                    status_icon,
                    formatted_date,
                    pickup_time_str,
                    leg.pickup_location[:20]
                    + ("..." if len(leg.pickup_location) > 20 else ""),
                    leg.dropoff_location[:20]
                    + ("..." if len(leg.dropoff_location) > 20 else ""),
                    driver_info,
                )
            )

        return format_html("<br>".join(result))

    @admin.display(description="Customer")
    def customer_link(self, obj):
        if not obj.customer:
            return "-"
        url = reverse("admin:reservations_customer_change", args=[obj.customer.id])
        return format_html(
            '<a href="{}">{} {}</a>',
            url,
            obj.customer.first_name,
            obj.customer.last_name,
        )

    @admin.display(description="Price")
    def total_price_display(self, obj):
        return f"${obj.total_price:.2f}"

    @admin.display(description="Vehicle")
    def vehicle_display(self, obj):
        if not obj.vehicle:
            return "-"
        return format_html('<span style="font-weight: bold;">{}</span>', obj.vehicle)

    @admin.display(description="Status")
    def status_with_color(self, obj):
        colors = {
            "pending": "#FFC107",  # Yellow
            "confirmed": "#4CAF50",  # Green
            "completed": "#2196F3",  # Blue
            "cancelled": "#F44336",  # Red
        }

        color = colors.get(obj.status, "gray")
        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 4px;">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.display(description="Travel Agent")
    def agent_info(self, obj):
        if not obj.travel_agent:
            return "-"

        agent = obj.travel_agent
        url = reverse("admin:users_travelagent_change", args=[agent.id])

        commission_status = "Paid" if obj.commission_paid else "Unpaid"
        commission_color = "green" if obj.commission_paid else "red"

        return format_html(
            '<a href="{}">{}</a><br/><span style="color: {};">${} ({})</span>',
            url,
            agent,
            commission_color,
            obj.commission_amount,
            commission_status,
        )

    @admin.display(description="Payment Status")
    def payment_status_display(self, obj):
        # Check if payments related manager exists and has items
        if not hasattr(obj, "payments") or not obj.payments.exists():
            return "-"

        # Get the last payment
        payment = obj.payments.last()

        # Generate the correct URL to the Payment admin page
        payment_url = reverse("admin:payment_payment_change", args=[payment.id])

        # Define status to color mapping
        status_color = {
            "paid": "green",
            "pending": "orange",
            "failed": "red",
            "card_saved": "blue",  # New status for 'card saved'
        }

        # Get color based on payment status
        colour = status_color.get(
            payment.status,
            "gray",  # Default to gray for unknown statuses
        )

        # Return a formatted display with a clickable link to the Payment page
        return format_html(
            '<a href="{}" style="color:{};font-weight:bold;">{}</a><br/>Amount: ${}<br/>Type: {}',
            payment_url,  # Link to the Payment admin page
            colour,
            payment.status.capitalize(),
            payment.amount,
            payment.payment_type.replace("_", " ").title(),
        )

    @admin.display(description="Profit")
    def profit_display(self, obj):
        """Display profit with color coding based on percentage"""
        if not hasattr(obj, "profit_estimate") or obj.profit_estimate is None:
            # Calculate on the fly if not stored
            profit = obj.calculate_profit()
        else:
            profit = obj.profit_estimate

        # Calculate percentage for color
        if obj.total_price and obj.total_price > 0:
            percentage = (profit / obj.total_price) * 100
        else:
            percentage = 0

        # Color code based on profit margin
        if percentage >= 40:
            color = "green"
        elif percentage >= 20:
            color = "orange"
        else:
            color = "red"

        # Format numbers first as strings
        profit_str = f"${profit}"
        percentage_str = f"{percentage:.1f}%"

        # Then use format_html without trying to format floats
        return format_html(
            '<span style="color: {};">{} ({})</span>', color, profit_str, percentage_str
        )

    @admin.display(description="Profit %")
    def profit_percentage(self, obj):
        if obj.total_price and obj.total_price > 0:
            profit = obj.calculate_profit()
            percentage = (profit / obj.total_price) * 100
            return f"{percentage:.1f}%"
        return "N/A"

    profit_percentage.short_description = "Profit %"

    @admin.display(description="Source")
    def utm_source_display(self, obj):
        if obj.utm_source:
            return obj.utm_source
        return "—"

    utm_source_display.short_description = "Source"

    @admin.display(description="Campaign")
    def utm_campaign_display(self, obj):
        if obj.utm_campaign:
            return obj.utm_campaign
        return "—"

    utm_campaign_display.short_description = "Campaign"

    @admin.display(description="Google Click ID")
    def gclid_display(self, obj):
        if obj.gclid:
            return obj.gclid[:20] + "..." if len(obj.gclid) > 20 else obj.gclid
        return "—"

    # ── Actions ────────────────────────────────────────────
    @admin.action(description="Mark selected reservations as confirmed")
    def mark_as_confirmed(self, request, queryset):
        updated = queryset.update(status="confirmed")
        self.message_user(
            request, f"{updated} reservations have been marked as confirmed."
        )

    @admin.action(description="Mark selected reservations as completed")
    def mark_as_completed(self, request, queryset):
        updated = queryset.update(status="completed")
        # Also update all legs for these reservations
        from django.db.models import F

        Leg.objects.filter(reservation__in=queryset).update(
            status=F("reservation__status")
        )
        self.message_user(
            request, f"{updated} reservations have been marked as completed."
        )

    @admin.action(description="Mark selected reservations as cancelled")
    def mark_as_cancelled(self, request, queryset):
        updated = queryset.update(status="cancelled")
        self.message_user(
            request, f"{updated} reservations have been marked as cancelled."
        )

    @admin.action(description="Update profit calculations")
    def update_profit_calculations(self, request, queryset):
        """Recalculate and update profit for selected reservations"""
        for reservation in queryset:
            reservation.update_profit_calculations()

        self.message_user(
            request, f"Profit calculations updated for {queryset.count()} reservations."
        )


@admin.register(Leg)
class LegAdmin(ImportExportModelAdmin):
    resource_class = LegResource
    form = LegAdminForm
    list_display = (
        "pickup_date_display",
        "pickup_time",
        "reservation_link",
        "customer_display",
        "vehicle_display",
        "pickup_location",
        "dropoff_location",
        "driver",
        "driver_pay_amount",
        "revenue_share_display",
        "profit_display",
        "payment_status",
        "status_display",
        "driver_notes_display",
    )
    list_filter = (
        "pickup_date",
        DriverAssignmentFilter,
        "reservation__status",
        "status",
        "payment_status",
    )
    search_fields = (
        "pickup_location",
        "dropoff_location",
        "reservation__customer__first_name",
        "reservation__customer__last_name",
        "driver__username",
    )
    ordering = ("pickup_date", "pickup_time")
    list_editable = ("driver", "driver_pay_amount", "payment_status")
    list_per_page = 50
    autocomplete_fields = ("reservation",)

    actions = [
        "assign_driver",
        "mark_as_completed",
        "set_payment_status_paid",
        "set_payment_status_unpaid",
    ]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related(
            "reservation",
            "reservation__customer",
            "reservation__vehicle",
            "driver",
            "flight_information",
        )

    @admin.display(description="Pickup Date")
    def pickup_date_display(self, obj):
        if not obj.pickup_date:
            return "-"

        today = timezone.localdate()

        if obj.pickup_date == today:
            return format_html(
                '<span style="color: red; font-weight: bold;">TODAY</span>'
            )
        elif obj.pickup_date == (today + timedelta(days=1)):
            return format_html(
                '<span style="color: orange; font-weight: bold;">TOMORROW</span>'
            )

        # For dates within a week, highlight them
        if obj.pickup_date < today:
            return format_html(
                '<span style="color: gray;">{}</span>',
                obj.pickup_date.strftime("%a, %b %d"),
            )
        elif (obj.pickup_date - today).days < 7:
            return format_html(
                '<span style="font-weight: bold;">{}</span>',
                obj.pickup_date.strftime("%a, %b %d"),
            )

        return obj.pickup_date.strftime("%a, %b %d")

    @admin.display(description="Reservation")
    def reservation_link(self, obj):
        if obj.reservation:
            url = reverse(
                "admin:reservations_reservation_change", args=[obj.reservation.id]
            )
            return format_html('<a href="{}">{}</a>', url, obj.reservation.id)
        return "-"

    @admin.display(description="Customer")
    def customer_display(self, obj):
        if obj.reservation and obj.reservation.customer:
            customer = obj.reservation.customer
            url = reverse("admin:reservations_customer_change", args=[customer.id])
            return format_html(
                '<a href="{}">{} {}</a>', url, customer.first_name, customer.last_name
            )
        return "-"

    @admin.display(description="Status")
    def status_display(self, obj):
        status = obj.status or (obj.reservation.status if obj.reservation else "-")

        colors = {
            "pending": "#FFC107",  # Yellow
            "confirmed": "#4CAF50",  # Green
            "completed": "#2196F3",  # Blue
            "cancelled": "#F44336",  # Red
        }

        color = colors.get(status, "gray")

        return format_html(
            '<span style="color: white; background-color: {}; padding: 3px 8px; border-radius: 4px;">{}</span>',
            color,
            status.capitalize() if status != "-" else "-",
        )

    @admin.display(description="Vehicle")
    def vehicle_display(self, obj):
        if obj.reservation and obj.reservation.vehicle:
            return obj.reservation.vehicle.get_vehicle_type_display()
        return "-"

    @admin.display(description="Driver")
    def driver_display(self, obj):
        if not obj.driver:
            return format_html('<span style="color: red;">Not Assigned</span>')

        return format_html('<span style="color: green;">{}</span>', obj.driver)

    @admin.action(description="Assign driver to selected legs")
    def assign_driver(self, request, queryset):
        # Implementation would redirect to a form to select a driver
        pass

    @admin.action(description="Mark selected legs as completed")
    def mark_as_completed(self, request, queryset):
        updated = queryset.update(status="completed")
        self.message_user(request, f"{updated} legs have been marked as completed.")

    @admin.display(description="Revenue")
    def revenue_share_display(self, obj):
        if not obj.revenue_share and obj.reservation:
            # Calculate on the fly if not stored
            total_legs = obj.reservation.legs.count()
            if total_legs > 0:
                revenue_share = obj.reservation.total_price / total_legs
            else:
                revenue_share = 0
        else:
            revenue_share = obj.revenue_share or 0

        return f"${revenue_share}"

    @admin.display(description="Profit")
    def profit_display(self, obj):
        revenue = 0
        if hasattr(obj, "revenue_share") and obj.revenue_share:
            revenue = obj.revenue_share
        elif obj.reservation:
            # Calculate on the fly if not stored
            total_legs = obj.reservation.legs.count()
            if total_legs > 0:
                revenue = obj.reservation.total_price / total_legs

        driver_pay = obj.driver_pay_amount or 0
        profit = revenue - driver_pay

        # Color code based on profit amount
        if profit > 0:
            color = "green"
        else:
            color = "red"

        return format_html('<span style="color: {};">${}</span>', color, profit)

    @admin.action(description="Mark selected legs as paid")
    def set_payment_status_paid(self, request, queryset):
        updated = queryset.update(payment_status="paid")
        self.message_user(request, f"Payment status updated for {updated} legs.")

    @admin.action(description="Mark selected legs as unpaid")
    def set_payment_status_unpaid(self, request, queryset):
        updated = queryset.update(payment_status="unpaid")
        self.message_user(request, f"Payment status updated for {updated} legs.")

    @admin.display(description="Driver Notes")
    def driver_notes_display(self, obj):
        if obj.driver_notes:
            return (
                obj.driver_notes[:50] + "..."
                if len(obj.driver_notes) > 50
                else obj.driver_notes
            )
        return "-"


@admin.register(Flight)
class FlightAdmin(admin.ModelAdmin):
    list_display = ("airline", "flight_number", "flight_type")
    list_filter = ("flight_type", "airline")
    search_fields = ("airline", "flight_number")
    ordering = ("airline", "flight_number")


class QuoteInline(admin.TabularInline):
    model = Quote
    extra = 0
    readonly_fields = ("created_at", "updated_at")
    fields = (
        "pickup_location",
        "dropoff_location",
        "pickup_date",
        "vehicle",
        "trip_type",
        "estimated_price",
        "status",
        "is_current",
        "created_at",
    )
    ordering = ("-created_at",)


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    inlines = [QuoteInline]
    list_display = (
        "full_name",
        "contact_info",
        "trip_summary",
        "status_display",
        "priority_display",
        "pickup_date",
        "follow_up_date",
        "quote_requests_count",
        "created_at",
    )

    list_filter = (
        "status",
        "priority",
        "converted",
        "pickup_date",
        "created_at",
        MultipleQuotesFilter,
    )

    search_fields = (
        "first_name",
        "last_name",
        "email",
        "phone",
        "pickup_location",
        "dropoff_location",
    )

    list_per_page = 25
    date_hierarchy = "created_at"

    fieldsets = (
        (
            "Contact Information",
            {"fields": ("first_name", "last_name", "email", "phone")},
        ),
        (
            "Trip Details",
            {
                "fields": (
                    "pickup_location",
                    "dropoff_location",
                    "pickup_date",
                    "vehicle",
                    "trip_type",
                    "estimated_price",
                )
            },
        ),
        (
            "Lead Management",
            {"fields": ("status", "priority", "next_follow_up", "contact_attempts")},
        ),
        ("Notes", {"fields": ("notes",)}),
        (
            "System Info",
            {
                "fields": ("created_at", "converted", "converted_at"),
                "classes": ("collapse",),
            },
        ),
    )

    readonly_fields = ("created_at", "converted_at")

    actions = [
        "mark_contacted",
        "mark_interested",
        "mark_converted",
        "mark_lost",
        "set_high_priority",
        "schedule_follow_up_tomorrow",
        "schedule_follow_up_week",
        "identify_duplicates",
    ]

    # Display Methods
    @admin.display(description="Name", ordering="last_name")
    def full_name(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        if obj.converted:
            return format_html(
                '<span style="color: #28a745; font-weight: bold;">✓ {}</span>', name
            )
        return name or "No Name"

    @admin.display(description="Contact")
    def contact_info(self, obj):
        email = obj.email or "No email"
        phone = obj.phone or "No phone"
        return format_html(
            '<div style="font-size: 0.9em;"><div>{}</div><div>{}</div></div>',
            email,
            phone,
        )

    @admin.display(description="Trip Details")
    def trip_summary(self, obj):
        # Get the latest quote for this lead
        latest_quote = obj.latest_quote

        if latest_quote:
            date_str = (
                latest_quote.pickup_date.strftime("%b %d")
                if latest_quote.pickup_date
                else "No date"
            )
            arrow = "→" if latest_quote.trip_type == "oneway" else "⇄"
            location = f"{latest_quote.pickup_location or 'Unknown'} {arrow} {latest_quote.dropoff_location or 'Unknown'}"
            price = (
                f"${latest_quote.estimated_price:,.0f}"
                if latest_quote.estimated_price
                else "No price"
            )
            vehicle = (
                latest_quote.vehicle.vehicle_type
                if latest_quote.vehicle
                else "No vehicle"
            )
        else:
            # Fallback to lead data if no quotes exist
            date_str = (
                obj.pickup_date.strftime("%b %d") if obj.pickup_date else "No date"
            )
            arrow = "→" if obj.trip_type == "oneway" else "⇄"
            location = f"{obj.pickup_location or 'Unknown'} {arrow} {obj.dropoff_location or 'Unknown'}"
            price = (
                f"${obj.estimated_price:,.0f}" if obj.estimated_price else "No price"
            )
            vehicle = obj.vehicle.vehicle_type if obj.vehicle else "No vehicle"

        return format_html(
            '<div style="font-size: 0.9em;">'
            "<div><strong>{}</strong></div>"
            "<div>{}</div>"
            '<div style="color: #007bff;">{}</div>'
            '<div style="color: #6c757d; font-size: 0.8em;">{}</div></div>',
            date_str,
            location,
            price,
            vehicle,
        )

    @admin.display(description="Status", ordering="status")
    def status_display(self, obj):
        colors = {
            "new": "#6c757d",
            "contacted": "#007bff",
            "interested": "#28a745",
            "future_contact": "#17a2b8",
            "converted": "#28a745",
            "lost": "#dc3545",
        }

        color = colors.get(obj.status, "#6c757d")
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; border-radius: 3px; font-size: 0.8em; font-weight: bold;">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.display(description="Priority", ordering="priority")
    def priority_display(self, obj):
        colors = {
            "low": "#6c757d",
            "medium": "#ffc107",
            "high": "#dc3545",
            "urgent": "#dc3545",
        }

        color = colors.get(obj.priority, "#6c757d")
        text_color = "white" if obj.priority in ["high", "urgent"] else "black"

        return format_html(
            '<span style="background-color: {}; color: {}; padding: 2px 6px; border-radius: 3px; font-size: 0.8em; font-weight: bold;">{}</span>',
            color,
            text_color,
            obj.get_priority_display().upper(),
        )

    @admin.display(description="Follow-Up", ordering="next_follow_up")
    def follow_up_date(self, obj):
        if not obj.next_follow_up:
            return "-"

        now = timezone.now()
        due_date = obj.next_follow_up.date()
        today = now.date()

        if due_date < today:
            return format_html(
                '<span style="color: #dc3545; font-weight: bold;">OVERDUE</span>'
            )
        elif due_date == today:
            return format_html(
                '<span style="color: #fd7e14; font-weight: bold;">TODAY</span>'
            )
        else:
            return obj.next_follow_up.strftime("%b %d")

    @admin.display(description="Quote Requests")
    def quote_requests_count(self, obj):
        """Show how many quote requests this lead has made"""
        return obj.quote_count

    # Actions
    @admin.action(description="Mark as Contacted")
    def mark_contacted(self, request, queryset):
        updated = queryset.filter(status="new").update(
            status="contacted", contact_attempts=1, last_contact_date=timezone.now()
        )
        self.message_user(request, f"Marked {updated} leads as contacted.")

    @admin.action(description="Mark as Interested")
    def mark_interested(self, request, queryset):
        updated = queryset.exclude(status__in=["converted", "lost"]).update(
            status="interested"
        )
        self.message_user(request, f"Marked {updated} leads as interested.")

    @admin.action(description="Mark as Converted")
    def mark_converted(self, request, queryset):
        count = 0
        for lead in queryset.exclude(converted=True):
            lead.status = "converted"
            lead.converted = True
            lead.converted_at = timezone.now()
            lead.next_follow_up = None
            lead.save()
            count += 1
        self.message_user(request, f"Converted {count} leads.")

    @admin.action(description="Mark as Lost")
    def mark_lost(self, request, queryset):
        updated = queryset.exclude(status="lost").update(
            status="lost", converted=False, next_follow_up=None
        )
        self.message_user(request, f"Marked {updated} leads as lost.")

    @admin.action(description="Set High Priority")
    def set_high_priority(self, request, queryset):
        updated = queryset.update(priority="high")
        self.message_user(request, f"Set {updated} leads to high priority.")

    @admin.action(description="Follow-up Tomorrow")
    def schedule_follow_up_tomorrow(self, request, queryset):
        tomorrow = timezone.now() + timedelta(days=1)
        updated = queryset.exclude(status__in=["converted", "lost"]).update(
            next_follow_up=tomorrow
        )
        self.message_user(request, f"Scheduled follow-up for {updated} leads.")

    @admin.action(description="Follow-up Next Week")
    def schedule_follow_up_week(self, request, queryset):
        next_week = timezone.now() + timedelta(days=7)
        updated = queryset.exclude(status__in=["converted", "lost"]).update(
            next_follow_up=next_week
        )
        self.message_user(request, f"Scheduled follow-up for {updated} leads.")


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = (
        "lead_name",
        "trip_details",
        "vehicle_display",
        "price_display",
        "status_display",
        "pickup_date",
        "is_current_display",
        "created_at",
    )

    list_filter = (
        "status",
        "trip_type",
        "is_current",
        "pickup_date",
        "created_at",
    )

    search_fields = (
        "lead__first_name",
        "lead__last_name",
        "lead__email",
        "lead__phone",
        "pickup_location",
        "dropoff_location",
    )

    list_per_page = 25
    date_hierarchy = "created_at"

    fieldsets = (
        (
            "Lead Information",
            {"fields": ("lead",)},
        ),
        (
            "Trip Details",
            {
                "fields": (
                    "pickup_location",
                    "dropoff_location",
                    "pickup_date",
                    "trip_type",
                    "vehicle",
                    "estimated_price",
                )
            },
        ),
        (
            "Quote Management",
            {"fields": ("status", "is_current")},
        ),
        (
            "System Info",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Lead")
    def lead_name(self, obj):
        return obj.lead.get_full_name

    @admin.display(description="Trip")
    def trip_details(self, obj):
        arrow = "→" if obj.trip_type == "oneway" else "⇄"
        return f"{obj.pickup_location or 'Unknown'} {arrow} {obj.dropoff_location or 'Unknown'}"

    @admin.display(description="Vehicle")
    def vehicle_display(self, obj):
        return obj.vehicle.vehicle_type if obj.vehicle else "No vehicle"

    @admin.display(description="Price")
    def price_display(self, obj):
        return f"${obj.estimated_price:,.0f}" if obj.estimated_price else "No price"

    @admin.display(description="Status")
    def status_display(self, obj):
        colors = {
            "pending": "#ffc107",
            "sent": "#007bff",
            "accepted": "#28a745",
            "rejected": "#dc3545",
            "expired": "#6c757d",
        }
        color = colors.get(obj.status, "#6c757d")
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; border-radius: 3px; font-size: 0.8em; font-weight: bold;">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.display(description="Current")
    def is_current_display(self, obj):
        if obj.is_current:
            return format_html(
                '<span style="color: #28a745; font-weight: bold;">✓</span>'
            )
        return "-"
