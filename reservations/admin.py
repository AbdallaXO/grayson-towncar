# reservations/admin.py
from datetime import timedelta
import logging

from django.contrib import admin
from django import forms
from django.utils.html import format_html
from django.utils import timezone
from django.db.models import Min, Count, Q, Max
from django.urls import reverse
from django.contrib.admin import SimpleListFilter
from django.contrib.admin.actions import delete_selected
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import render
from rates.models import Vehicle
from import_export import resources, fields
from import_export.admin import ImportExportModelAdmin
from .models import Customer, Reservation, Leg, Flight, Cruise, Lead, Quote
from django.db import models

logger = logging.getLogger(__name__)


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
                    "cruise_information",
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
            ("next90", "Next 90 days"),
            ("next120", "Next 120 days"),
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
        if self.value() == "next90":
            return qs.filter(
                earliest_leg_date__range=(today, today + timedelta(days=90))
            )
        if self.value() == "next120":
            return qs.filter(
                earliest_leg_date__range=(today, today + timedelta(days=120))
            )
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



# ─── Custom Filters ─────────────────────────────────────────────────────
class LeadTripDateFilter(SimpleListFilter):
    title = "trip date range"
    parameter_name = "trip_date_range"

    def lookups(self, request, model_admin):
        return (
            ("today", "🚨 Today"),
            ("tomorrow", "⚡ Tomorrow"),
            ("next_3_days", "⚡ Next 3 Days"),
            ("next_7_days", "🔔 Next 7 Days"),
            ("next_14_days", "📅 Next 14 Days"),
            ("next_30_days", "📅 Next 30 Days"),
            ("next_60_days", "📅 Next 60 Days"),
            ("next_90_days", "📅 Next 90 Days"),
            ("next_120_days", "📅 Next 120 Days"),
            ("this_week", "📅 This Week"),
            ("next_week", "📅 Next Week"),
            ("this_month", "📅 This Month"),
            ("next_month", "📅 Next Month"),
            ("no_date", "❓ No Date Set"),
        )

    def queryset(self, request, qs):
        today = timezone.now().date()
        
        if self.value() == "today":
            return qs.filter(pickup_date=today)
        
        elif self.value() == "tomorrow":
            tomorrow = today + timedelta(days=1)
            return qs.filter(pickup_date=tomorrow)
        
        elif self.value() == "next_3_days":
            end_date = today + timedelta(days=3)
            return qs.filter(
                pickup_date__gte=today,
                pickup_date__lte=end_date
            )
        
        elif self.value() == "next_7_days":
            end_date = today + timedelta(days=7)
            return qs.filter(
                pickup_date__gte=today,
                pickup_date__lte=end_date
            )
        
        elif self.value() == "next_14_days":
            end_date = today + timedelta(days=14)
            return qs.filter(
                pickup_date__gte=today,
                pickup_date__lte=end_date
            )
        
        elif self.value() == "next_30_days":
            end_date = today + timedelta(days=30)
            return qs.filter(
                pickup_date__gte=today,
                pickup_date__lte=end_date
            )
        
        elif self.value() == "next_60_days":
            end_date = today + timedelta(days=60)
            return qs.filter(
                pickup_date__gte=today,
                pickup_date__lte=end_date
            )
        
        elif self.value() == "next_90_days":
            end_date = today + timedelta(days=90)
            return qs.filter(
                pickup_date__gte=today,
                pickup_date__lte=end_date
            )
        
        elif self.value() == "next_120_days":
            end_date = today + timedelta(days=120)
            return qs.filter(
                pickup_date__gte=today,
                pickup_date__lte=end_date
            )
        
        elif self.value() == "this_week":
            # Get start and end of current week (Monday to Sunday)
            days_since_monday = today.weekday()
            start_of_week = today - timedelta(days=days_since_monday)
            end_of_week = start_of_week + timedelta(days=6)
            return qs.filter(
                pickup_date__gte=start_of_week,
                pickup_date__lte=end_of_week
            )
        
        elif self.value() == "next_week":
            # Get start and end of next week (Monday to Sunday)
            days_since_monday = today.weekday()
            start_of_week = today - timedelta(days=days_since_monday)
            start_of_next_week = start_of_week + timedelta(days=7)
            end_of_next_week = start_of_next_week + timedelta(days=6)
            return qs.filter(
                pickup_date__gte=start_of_next_week,
                pickup_date__lte=end_of_next_week
            )
        
        elif self.value() == "this_month":
            # Get start and end of current month
            start_of_month = today.replace(day=1)
            if today.month == 12:
                end_of_month = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
            else:
                end_of_month = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
            return qs.filter(
                pickup_date__gte=start_of_month,
                pickup_date__lte=end_of_month
            )
        
        elif self.value() == "next_month":
            # Get start and end of next month
            if today.month == 12:
                start_of_next_month = today.replace(year=today.year + 1, month=1, day=1)
            else:
                start_of_next_month = today.replace(month=today.month + 1, day=1)
            
            if start_of_next_month.month == 12:
                end_of_next_month = start_of_next_month.replace(year=start_of_next_month.year + 1, month=1, day=1) - timedelta(days=1)
            else:
                end_of_next_month = start_of_next_month.replace(month=start_of_next_month.month + 1, day=1) - timedelta(days=1)
            
            return qs.filter(
                pickup_date__gte=start_of_next_month,
                pickup_date__lte=end_of_next_month
            )
        
        elif self.value() == "no_date":
            return qs.filter(pickup_date__isnull=True)
        
        return qs


class LeadFollowUpFilter(SimpleListFilter):
    title = "follow-up status"
    parameter_name = "follow_up_status"

    def lookups(self, request, model_admin):
        return (
            ("overdue", "🚨 Overdue Follow-up"),
            ("due_today", "⚡ Due Today"),
            ("due_tomorrow", "🔔 Due Tomorrow"),
            ("due_this_week", "📅 Due This Week"),
            ("due_next_week", "📅 Due Next Week"),
            ("no_follow_up", "❓ No Follow-up Scheduled"),
            ("completed", "✅ Follow-up Completed"),
        )

    def queryset(self, request, qs):
        today = timezone.now().date()
        
        if self.value() == "overdue":
            return qs.filter(
                next_follow_up__lt=today
            ).exclude(status__in=["converted", "lost"])
        
        elif self.value() == "due_today":
            return qs.filter(
                next_follow_up__date=today
            )
        
        elif self.value() == "due_tomorrow":
            tomorrow = today + timedelta(days=1)
            return qs.filter(
                next_follow_up__date=tomorrow
            )
        
        elif self.value() == "due_this_week":
            end_of_week = today + timedelta(days=6)
            return qs.filter(
                next_follow_up__date__gte=today,
                next_follow_up__date__lte=end_of_week
            )
        
        elif self.value() == "due_next_week":
            start_of_next_week = today + timedelta(days=7)
            end_of_next_week = today + timedelta(days=13)
            return qs.filter(
                next_follow_up__date__gte=start_of_next_week,
                next_follow_up__date__lte=end_of_next_week
            )
        
        elif self.value() == "no_follow_up":
            return qs.filter(next_follow_up__isnull=True)
        
        elif self.value() == "completed":
            return qs.filter(
                next_follow_up__isnull=False,
                last_contact_date__gte=models.F('next_follow_up')
            )
        
        return qs

class LeadSourceFilter(SimpleListFilter):
    title = "lead source"
    parameter_name = "lead_source"

    def lookups(self, request, model_admin):
        return (
            ("website", "🌐 Website"),
            ("phone", "📞 Phone"),
            ("email", "📧 Email"),
            ("referral", "👥 Referral"),
            ("social_media", "📱 Social Media"),
            ("google_ads", "🔍 Google Ads"),
            ("facebook_ads", "📘 Facebook Ads"),
            ("other", "❓ Other"),
            ("unknown", "❓ Unknown"),
        )

    def queryset(self, request, qs):
        if self.value() == "website":
            return qs.filter(
                Q(source="website") | Q(source__icontains="web")
            )
        elif self.value() == "phone":
            return qs.filter(
                Q(source="phone") | Q(source__icontains="call")
            )
        elif self.value() == "email":
            return qs.filter(
                Q(source="email") | Q(source__icontains="mail")
            )
        elif self.value() == "referral":
            return qs.filter(
                Q(source="referral") | Q(source__icontains="refer")
            )
        elif self.value() == "social_media":
            return qs.filter(
                Q(source__icontains="social") | 
                Q(source__icontains="facebook") | 
                Q(source__icontains="instagram") |
                Q(source__icontains="twitter")
            )
        elif self.value() == "google_ads":
            return qs.filter(
                Q(source__icontains="google") | Q(source__icontains="ads")
            )
        elif self.value() == "facebook_ads":
            return qs.filter(
                Q(source__icontains="facebook") | Q(source__icontains="fb")
            )
        elif self.value() == "other":
            return qs.filter(
                source__isnull=False
            ).exclude(
                Q(source__icontains="website") |
                Q(source__icontains="phone") |
                Q(source__icontains="email") |
                Q(source__icontains="referral") |
                Q(source__icontains="social") |
                Q(source__icontains="google") |
                Q(source__icontains="facebook")
            )
        elif self.value() == "unknown":
            return qs.filter(source__isnull=True)
        
        return qs


class LeadValueFilter(SimpleListFilter):
    title = "estimated value"
    parameter_name = "estimated_value"

    def lookups(self, request, model_admin):
        return (
            ("high_value", "💰 High Value ($500+)"),
            ("medium_value", "💵 Medium Value ($200-$499)"),
            ("low_value", "💸 Low Value (<$200)"),
            ("no_price", "❓ No Price Set"),
        )

    def queryset(self, request, qs):
        if self.value() == "high_value":
            return qs.filter(
                estimated_price__gte=500
            )
        elif self.value() == "medium_value":
            return qs.filter(
                estimated_price__gte=200,
                estimated_price__lt=500
            )
        elif self.value() == "low_value":
            return qs.filter(
                estimated_price__lt=200
            )
        elif self.value() == "no_price":
            return qs.filter(
                estimated_price__isnull=True
            )
        
        return qs


class ReservationSourceFilter(SimpleListFilter):
    title = "source"
    parameter_name = "utm_source"

    def lookups(self, request, model_admin):
        # Get all unique UTM sources from the database
        sources = Reservation.objects.exclude(
            utm_source__isnull=True
        ).exclude(
            utm_source__exact=""
        ).values_list('utm_source', flat=True).distinct().order_by('utm_source')
        
        lookups = []
        for source in sources:
            if source:  # Make sure source is not empty
                lookups.append((source, source))
        
        # Add option for reservations with no source
        lookups.append(("no_source", "No Source"))
        
        return lookups

    def queryset(self, request, qs):
        if self.value() == "no_source":
            return qs.filter(
                Q(utm_source__isnull=True) | Q(utm_source__exact="")
            )
        elif self.value():
            return qs.filter(utm_source=self.value())
        
        return qs


class ReservationCreatedAtFilter(SimpleListFilter):
    title = "created date"
    parameter_name = "created_at_range"

    def lookups(self, request, model_admin):
        current_year = timezone.now().year
        
        # Base lookups
        lookups = [
            ("today", "📅 Today"),
            ("yesterday", "📅 Yesterday"),
            ("this_week", "📅 This Week"),
            ("last_week", "📅 Last Week"),
            ("this_month", "📅 This Month"),
            ("last_month", "📅 Last Month"),
            ("last_30_days", "📅 Last 30 Days"),
            ("last_60_days", "📅 Last 60 Days"),
            ("last_90_days", "📅 Last 90 Days"),
            ("this_quarter", "📅 This Quarter"),
            ("last_quarter", "📅 Last Quarter"),
            ("this_year", "📅 This Year"),
            ("last_year", "📅 Last Year"),
        ]
        
        # Add current year months only
        months = [
            ("january", "January"),
            ("february", "February"), 
            ("march", "March"),
            ("april", "April"),
            ("may", "May"),
            ("june", "June"),
            ("july", "July"),
            ("august", "August"),
            ("september", "September"),
            ("october", "October"),
            ("november", "November"),
            ("december", "December"),
        ]
        
        # Add current year months only
        for month_key, month_name in months:
            lookups.append((f"{month_key}_{current_year}", f"📅 {month_name} {current_year}"))
        
        return lookups

    def queryset(self, request, qs):
        today = timezone.now().date()
        
        if self.value() == "today":
            return qs.filter(created_at__date=today)
        
        elif self.value() == "yesterday":
            yesterday = today - timedelta(days=1)
            return qs.filter(created_at__date=yesterday)
        
        elif self.value() == "this_week":
            # Get start of current week (Monday)
            days_since_monday = today.weekday()
            start_of_week = today - timedelta(days=days_since_monday)
            return qs.filter(created_at__date__gte=start_of_week)
        
        elif self.value() == "last_week":
            # Get start and end of last week
            days_since_monday = today.weekday()
            start_of_current_week = today - timedelta(days=days_since_monday)
            start_of_last_week = start_of_current_week - timedelta(days=7)
            end_of_last_week = start_of_current_week - timedelta(days=1)
            return qs.filter(
                created_at__date__gte=start_of_last_week,
                created_at__date__lte=end_of_last_week
            )
        
        elif self.value() == "this_month":
            start_of_month = today.replace(day=1)
            return qs.filter(created_at__date__gte=start_of_month)
        
        elif self.value() == "last_month":
            # Get start and end of last month
            if today.month == 1:
                start_of_last_month = today.replace(year=today.year - 1, month=12, day=1)
            else:
                start_of_last_month = today.replace(month=today.month - 1, day=1)
            
            start_of_current_month = today.replace(day=1)
            end_of_last_month = start_of_current_month - timedelta(days=1)
            
            return qs.filter(
                created_at__date__gte=start_of_last_month,
                created_at__date__lte=end_of_last_month
            )
        
        # Handle dynamic year-month filtering
        elif self.value() and "_" in self.value():
            try:
                month_year = self.value().split("_")
                if len(month_year) == 2:
                    month_name, year_str = month_year
                    year = int(year_str)
                    
                    # Map month names to numbers
                    month_map = {
                        "january": 1, "february": 2, "march": 3, "april": 4,
                        "may": 5, "june": 6, "july": 7, "august": 8,
                        "september": 9, "october": 10, "november": 11, "december": 12
                    }
                    
                    if month_name in month_map:
                        month_num = month_map[month_name]
                        
                        # Calculate start and end of month
                        start_date = timezone.datetime(year, month_num, 1).date()
                        
                        # Calculate end of month
                        if month_num == 12:
                            end_date = timezone.datetime(year + 1, 1, 1).date() - timedelta(days=1)
                        else:
                            end_date = timezone.datetime(year, month_num + 1, 1).date() - timedelta(days=1)
                        
                        return qs.filter(
                            created_at__date__gte=start_date,
                            created_at__date__lte=end_date
                        )
            except (ValueError, KeyError):
                pass
        
        elif self.value() == "last_30_days":
            thirty_days_ago = today - timedelta(days=30)
            return qs.filter(created_at__date__gte=thirty_days_ago)
        
        elif self.value() == "last_60_days":
            sixty_days_ago = today - timedelta(days=60)
            return qs.filter(created_at__date__gte=sixty_days_ago)
        
        elif self.value() == "last_90_days":
            ninety_days_ago = today - timedelta(days=90)
            return qs.filter(created_at__date__gte=ninety_days_ago)
        
        elif self.value() == "this_quarter":
            # Calculate current quarter
            current_quarter = (today.month - 1) // 3 + 1
            quarter_start_month = (current_quarter - 1) * 3 + 1
            start_of_quarter = today.replace(month=quarter_start_month, day=1)
            return qs.filter(created_at__date__gte=start_of_quarter)
        
        elif self.value() == "last_quarter":
            # Calculate last quarter
            current_quarter = (today.month - 1) // 3 + 1
            if current_quarter == 1:
                last_quarter = 4
                last_quarter_year = today.year - 1
            else:
                last_quarter = current_quarter - 1
                last_quarter_year = today.year
            
            last_quarter_start_month = (last_quarter - 1) * 3 + 1
            start_of_last_quarter = timezone.datetime(last_quarter_year, last_quarter_start_month, 1).date()
            
            # End of last quarter
            if last_quarter == 4:
                end_of_last_quarter = timezone.datetime(last_quarter_year + 1, 1, 1).date() - timedelta(days=1)
            else:
                next_quarter_start_month = last_quarter * 3 + 1
                end_of_last_quarter = timezone.datetime(last_quarter_year, next_quarter_start_month, 1).date() - timedelta(days=1)
            
            return qs.filter(
                created_at__date__gte=start_of_last_quarter,
                created_at__date__lte=end_of_last_quarter
            )
        
        elif self.value() == "this_year":
            start_of_year = today.replace(month=1, day=1)
            return qs.filter(created_at__date__gte=start_of_year)
        
        elif self.value() == "last_year":
            start_of_last_year = today.replace(year=today.year - 1, month=1, day=1)
            start_of_this_year = today.replace(month=1, day=1)
            end_of_last_year = start_of_this_year - timedelta(days=1)
            return qs.filter(
                created_at__date__gte=start_of_last_year,
                created_at__date__lte=end_of_last_year
            )
        
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
                "legs", "legs__driver", "legs__flight_information", "legs__cruise_information", "payments"
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
        ReservationCreatedAtFilter,
        "trip_type",
        "status",
        CommissionStatusFilter,
        ReservationSourceFilter,
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

        # Get the latest payment (most recent by created_at)
        payment = obj.payments.order_by('-created_at').first()

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
            "cruise_information",
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


class FlightInUseFilter(SimpleListFilter):
    """Filter to show flights that are in use (linked to legs) vs orphaned."""
    title = 'in use'
    parameter_name = 'in_use'
    
    def lookups(self, request, model_admin):
        return (
            ('yes', 'In Use (Linked to Legs)'),
            ('no', 'Orphaned (Not Linked)'),
        )
    
    def queryset(self, request, queryset):
        from .models import Leg
        from django.db.models import Exists, OuterRef
        if self.value() == 'yes':
            # OneToOneField reverse relationship - flights that have a leg
            return queryset.filter(
                Exists(Leg.objects.filter(flight_information=OuterRef('pk')))
            )
        elif self.value() == 'no':
            # Flights not linked to any leg
            return queryset.exclude(
                Exists(Leg.objects.filter(flight_information=OuterRef('pk')))
            )
        return queryset


@admin.register(Flight)
class FlightAdmin(admin.ModelAdmin):
    list_display = ("airline", "airline_display_name", "flight_number", "flight_type", "leg_count", "is_in_use")
    list_filter = ("flight_type", "airline", FlightInUseFilter)
    search_fields = ("airline", "airline_display_name", "flight_number")
    ordering = ("airline", "flight_number")
    readonly_fields = ("airline_display_name", "leg_count", "is_in_use")
    
    def get_queryset(self, request):
        """Optimize queryset to check if flight is in use."""
        qs = super().get_queryset(request)
        # Check if flight is linked to any leg via subquery
        from .models import Leg
        from django.db.models import Exists, OuterRef
        return qs.annotate(
            is_linked=Exists(Leg.objects.filter(flight_information=OuterRef('pk')))
        )
    
    def leg_count(self, obj):
        """Show if flight is linked to a leg."""
        # OneToOneField means there can only be 0 or 1 leg per flight
        if hasattr(obj, 'is_linked'):
            return "Yes" if obj.is_linked else "No"
        # Fallback: check directly
        from .models import Leg
        return "Yes" if Leg.objects.filter(flight_information=obj).exists() else "No"
    
    leg_count.short_description = "Linked to Leg"
    
    def is_in_use(self, obj):
        """Show if flight is currently linked to any leg."""
        if hasattr(obj, 'is_linked'):
            in_use = obj.is_linked
        else:
            from .models import Leg
            in_use = Leg.objects.filter(flight_information=obj).exists()
        
        if in_use:
            return format_html('<span style="color: green;">✓ Yes</span>')
        return format_html('<span style="color: red;">✗ No</span>')
    
    is_in_use.short_description = "Status"


@admin.register(Cruise)
class CruiseAdmin(admin.ModelAdmin):
    list_display = ("cruise_line", "ship_name")
    list_filter = ("cruise_line",)
    search_fields = ("cruise_line", "ship_name")
    ordering = ("cruise_line", "ship_name")


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
    """
    Lead management with automatic conversion tracking.
    
    Features:
    - Automatic conversion when reservations are created (matching by email/phone)
    - Visual status indicators with background colors
    - Bulk actions for lead management
    - Conversion tracking and analytics
    """
    inlines = [QuoteInline]
    list_display = (
        "full_name",
        "contact_info",
        "trip_summary",
        "status_display",
        "priority_display",
        "pickup_date",
        "days_until_trip",
        "follow_up_date",
        "quote_requests_count",
        "initial_sms_sent",
        "has_replied",
        "ghl_synced",
        "created_at",
    )

    list_filter = (
        "status",
        "priority",
        "converted",
        "initial_sms_sent",
        "has_replied",
        "created_at",
        LeadTripDateFilter,
        LeadFollowUpFilter,
        LeadValueFilter,
    )

    search_fields = (
        "first_name",
        "last_name",
        "email",
        "phone",
        "pickup_location",
        "dropoff_location",
    )

    fieldsets = (
        ("Basic Information", {
            "fields": (
                ("first_name", "last_name"),
                ("email", "phone"),
                ("pickup_date", "trip_type"),
                ("pickup_location", "dropoff_location"),
                "notes",
            )
        }),
        ("Lead Details", {
            "fields": (
                ("status", "priority"),
                "vehicle",
                ("converted", "converted_at"),
                ("contact_attempts", "last_contact_date"),
                "next_follow_up",
            )
        }),
        ("Trip Information", {
            "fields": (
                "estimated_price",
            ),
            "classes": ("collapse",)
        }),
        ("System Information", {
            "fields": (
                "created_at",
            ),
            "classes": ("collapse",)
        }),
    )

    readonly_fields = (
        "created_at",
        "converted_at",
        "contact_attempts",
        "last_contact_date",
        "quote_count",
        "latest_quote",
    )

    list_per_page = 50
    list_max_show_all = 500
    date_hierarchy = "pickup_date"
    ordering = ("-created_at",)
    
    actions = [
        "mark_contacted",
        "mark_interested", 
        "mark_converted",
        "check_auto_conversion",
        "mark_lost",
        "set_high_priority",
        "set_medium_priority",
        "set_low_priority",
        "set_urgent_priority",
        "schedule_follow_up_week",
        "send_sms_to_selected",
        "sync_to_ghl_without_sms",
    ]

    def get_list_display(self, request):
        """Customize list display based on user preferences"""
        # Always return the default list display
        return self.list_display

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # Optimize queries to prevent N+1 problem
        qs = (
            qs.select_related("vehicle")
            .prefetch_related(
                "quotes",
                "quotes__vehicle"
            )
            .annotate(
                total_quotes=Count("quotes", distinct=True),
                latest_quote_date=Max("quotes__created_at")
            )
        )
        return qs

    def changelist_view(self, request, extra_context=None):
        """Customize the changelist view with additional context"""
        extra_context = extra_context or {}
        
        # Get stats from ALL leads (not just the current page)
        today = timezone.now().date()
        
        # Use the base model manager to get ALL leads, not filtered by admin
        from .models import Lead
        all_leads = Lead.objects.all()
        
        # Calculate summary statistics from all leads
        extra_context.update({
            "total_leads": all_leads.count(),
            "leads_tomorrow": all_leads.filter(pickup_date=today + timedelta(days=1)).count(),
            "leads_this_week": all_leads.filter(
                pickup_date__gte=today,
                pickup_date__lte=today + timedelta(days=6)
            ).count(),
            "leads_next_week": all_leads.filter(
                pickup_date__gte=today + timedelta(days=7),
                pickup_date__lte=today + timedelta(days=13)
            ).count(),
            "leads_next_30_days": all_leads.filter(
                pickup_date__gte=today,
                pickup_date__lte=today + timedelta(days=30)
            ).exclude(status__in=["converted", "lost"]).count(),
            "leads_next_60_days": all_leads.filter(
                pickup_date__gte=today,
                pickup_date__lte=today + timedelta(days=60)
            ).exclude(status__in=["converted", "lost"]).count(),
            "urgent_leads": all_leads.filter(
                pickup_date__gte=today,
                pickup_date__lte=today + timedelta(days=7)
            ).exclude(status__in=["converted", "lost"]).count(),
            "contacted_leads": all_leads.filter(status="contacted").count(),
            "new_leads": all_leads.filter(status="new").count(),
            "converted_leads": all_leads.filter(status="converted").count(),
            "lost_leads": all_leads.filter(status="lost").count(),
            "conversion_rate": round(
                (all_leads.filter(status="converted").count() / max(all_leads.count(), 1)) * 100, 1
            ),
        })
        
        return super().changelist_view(request, extra_context)

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
        # Use prefetched quotes to avoid additional queries
        quotes = obj.quotes.all()
        if quotes:
            # Get the latest quote (most recent created_at)
            latest_quote = max(quotes, key=lambda q: q.created_at) if quotes else None
            if latest_quote:
                pickup = latest_quote.pickup_location or "TBD"
                dropoff = latest_quote.dropoff_location or "TBD"
                vehicle = latest_quote.vehicle or "TBD"
                price = latest_quote.estimated_price or "TBD"
                
                return format_html(
                    '<div style="font-size: 0.9em;">'
                    '<div><strong>From:</strong> {}</div>'
                    '<div><strong>To:</strong> {}</div>'
                    '<div><strong>Vehicle:</strong> {}</div>'
                    '<div><strong>Price:</strong> ${}</div>'
                    '</div>',
                    pickup, dropoff, vehicle, price
                )
        return "No quote details"

    @admin.display(description="Status", ordering="status")
    def status_display(self, obj):
        status_colors = {
            "new": {"bg": "#ffeb3b", "text": "#000000"},  # Bright yellow background, black text
            "contacted": {"bg": "#2196f3", "text": "#ffffff"},  # Bright blue background, white text
            "interested": {"bg": "#00bcd4", "text": "#ffffff"},  # Bright cyan background, white text
            "future_contact": {"bg": "#ff9800", "text": "#ffffff"},  # Bright orange background, white text
            "converted": {"bg": "#4caf50", "text": "#ffffff"},  # Bright green background, white text
            "lost": {"bg": "#f44336", "text": "#ffffff"},  # Bright red background, white text
        }
        
        colors = status_colors.get(obj.status, {"bg": "#ffeb3b", "text": "#000000"})
        
        # Build the HTML content properly
        if obj.status == "converted" and obj.converted_at:
            return format_html(
                '<span style="background-color: {}; color: {}; font-weight: 900; font-size: 14px; padding: 8px 12px; border-radius: 8px; display: inline-block; min-width: 100px; text-align: center; text-transform: uppercase; letter-spacing: 1px; box-shadow: 0 2px 4px rgba(0,0,0,0.2); border: 2px solid {};">{}<br><small style="color: #ffffff; font-size: 11px; font-weight: bold;">Converted: {}</small></span>',
                colors["bg"],
                colors["text"],
                colors["bg"],
                obj.get_status_display(),
                obj.converted_at.strftime('%m/%d/%Y')
            )
        else:
            return format_html(
                '<span style="background-color: {}; color: {}; font-weight: 900; font-size: 14px; padding: 8px 12px; border-radius: 8px; display: inline-block; min-width: 100px; text-align: center; text-transform: uppercase; letter-spacing: 1px; box-shadow: 0 2px 4px rgba(0,0,0,0.2); border: 2px solid {};">{}</span>',
                colors["bg"],
                colors["text"],
                colors["bg"],
                obj.get_status_display()
            )

    @admin.display(description="Priority", ordering="priority")
    def priority_display(self, obj):
        priority_colors = {
            "low": {"bg": "#9e9e9e", "text": "#ffffff"},  # Bright grey background, white text
            "medium": {"bg": "#00bcd4", "text": "#ffffff"},  # Bright cyan background, white text
            "high": {"bg": "#ff9800", "text": "#ffffff"},  # Bright orange background, white text
            "urgent": {"bg": "#f44336", "text": "#ffffff"},  # Bright red background, white text
        }
        
        colors = priority_colors.get(obj.priority, {"bg": "#9e9e9e", "text": "#ffffff"})
        return format_html(
            '<span style="background-color: {}; color: {}; font-weight: 900; font-size: 14px; padding: 8px 12px; border-radius: 8px; display: inline-block; min-width: 100px; text-align: center; text-transform: uppercase; letter-spacing: 1px; box-shadow: 0 2px 4px rgba(0,0,0,0.2); border: 2px solid {};">{}</span>',
            colors["bg"],
            colors["text"],
            colors["bg"],
            obj.get_priority_display()
        )

    @admin.display(description="Days Until Trip", ordering="pickup_date")
    def days_until_trip(self, obj):
        if not obj.pickup_date:
            return format_html('<span style="color: #6c757d;">No date set</span>')
        
        today = timezone.now().date()
        days_diff = (obj.pickup_date - today).days
        
        if days_diff < 0:
            return format_html('<span style="color: #dc3545; font-weight: bold;">{} Days</span>', abs(days_diff))
        elif days_diff == 0:
            return format_html('<span style="color: #ff0018; font-weight: bold;">TODAY</span>')
        elif days_diff <= 3:
            return format_html('<span style="color: #ffc107; font-weight: bold;">{} days</span>', days_diff)
        elif days_diff <= 7:
            return format_html('<span style="color: #ffcc00; font-weight: bold;">{} days</span>', days_diff)
        else:
            return format_html('<span style="color: #000;">{} days</span>', days_diff)

    @admin.display(description="Urgency", ordering="priority")
    def urgency_indicator(self, obj):
        if not obj.pickup_date:
            if obj.priority in ["urgent", "high"]:
                return format_html('<span style="color: #dc3545; font-weight: bold;">⚠️ URGENT</span>')
            return format_html('<span style="color: #6c757d;">No date</span>')
        
        today = timezone.now().date()
        days_diff = (obj.pickup_date - today).days
        
        if days_diff < 0:
            return format_html('<span style="color: #dc3545; font-weight: bold;">🚨 OVERDUE</span>')
        elif days_diff == 0:
            return format_html('<span style="color: #dc3545; font-weight: bold;">🚨 TODAY</span>')
        elif days_diff <= 3:
            return format_html('<span style="color: #ffc107; font-weight: bold;">⚡ URGENT</span>')
        elif days_diff <= 7:
            return format_html('<span style="color: #17a2b8; font-weight: bold;">🔔 SOON</span>')
        else:
            return format_html('<span style="color: #28a745;">📅 Scheduled</span>')

    @admin.display(description="Follow-Up", ordering="next_follow_up")
    def follow_up_date(self, obj):
        if not obj.next_follow_up:
            return format_html('<span style="color: #6c757d;">No follow-up scheduled</span>')
        
        today = timezone.now().date()
        follow_up_date = obj.next_follow_up.date()
        days_diff = (follow_up_date - today).days
        
        if days_diff < 0:
            return format_html('<span style="color: #dc3545; font-weight: bold;">{} days overdue</span>', abs(days_diff))
        elif days_diff == 0:
            return format_html('<span style="color: #ffc107; font-weight: bold;">Due today</span>')
        elif days_diff == 1:
            return format_html('<span style="color: #17a2b8; font-weight: bold;">Due tomorrow</span>')
        elif days_diff <= 7:
            return format_html('<span style="color: #17a2b8;">Due in {} days</span>', days_diff)
        else:
            return format_html('<span style="color: #28a745;">Due in {} days</span>', days_diff)

    @admin.display(description="Quote Requests")
    def quote_requests_count(self, obj):
        # Use the annotation if available, otherwise fall back to the property
        count = getattr(obj, 'total_quotes', obj.quote_count)
        if count == 0:
            return format_html('<span style="color: #6c757d;">0</span>')
        elif count == 1:
            return format_html('<span style="color: #28a745;">1</span>')
        else:
            return format_html('<span style="color: #17a2b8; font-weight: bold;">{}</span>', count)

    @admin.display(description="GHL Synced", ordering="ghl_contact_id")
    def ghl_synced(self, obj):
        """Display checkmark if lead is synced to GHL, X if not."""
        if obj.ghl_contact_id:
            return format_html('<span style="color: #28a745; font-size: 16px;">✅</span>')
        else:
            return format_html('<span style="color: #dc3545; font-size: 16px;">❌</span>')

    # Actions
    @admin.action(description="Mark as Contacted")
    def mark_contacted(self, request, queryset):
        # Get leads with GHL contact IDs before update (for syncing)
        leads_with_ghl = list(queryset.filter(ghl_contact_id__isnull=False).values_list('id', 'ghl_contact_id'))
        
        # Update status
        updated = queryset.update(
            status="contacted",
            contact_attempts=models.F('contact_attempts') + 1,
            last_contact_date=timezone.now()
        )
        
        # Sync status to GHL for leads that have contact IDs
        if leads_with_ghl:
            from ghl_integration.services import GoHighLevelService
            from threading import Thread
            
            def sync_statuses_in_background():
                service = GoHighLevelService()
                synced_count = 0
                for lead_id, contact_id in leads_with_ghl:
                    try:
                        success = service.update_contact_status_fields(
                            contact_id=contact_id,
                            status="contacted"
                        )
                        if success:
                            synced_count += 1
                    except Exception as e:
                        logger.error(f"Failed to sync status to GHL for Lead #{lead_id}: {e}")
                
                logger.info(f"Synced status to GHL for {synced_count} out of {len(leads_with_ghl)} leads")
            
            thread = Thread(target=sync_statuses_in_background, daemon=True)
            thread.start()
        
        self.message_user(request, f"Marked {updated} leads as contacted.")

    @admin.action(description="Mark as Interested")
    def mark_interested(self, request, queryset):
        # Get leads with GHL contact IDs before update (for syncing)
        leads_with_ghl = list(queryset.filter(ghl_contact_id__isnull=False).values_list('id', 'ghl_contact_id'))
        
        # Update status
        updated = queryset.update(status="interested")
        
        # Sync status to GHL for leads that have contact IDs
        if leads_with_ghl:
            from ghl_integration.services import GoHighLevelService
            from threading import Thread
            
            def sync_statuses_in_background():
                service = GoHighLevelService()
                synced_count = 0
                for lead_id, contact_id in leads_with_ghl:
                    try:
                        success = service.update_contact_status_fields(
                            contact_id=contact_id,
                            status="interested"
                        )
                        if success:
                            synced_count += 1
                    except Exception as e:
                        logger.error(f"Failed to sync status to GHL for Lead #{lead_id}: {e}")
                
                logger.info(f"Synced status to GHL for {synced_count} out of {len(leads_with_ghl)} leads")
            
            thread = Thread(target=sync_statuses_in_background, daemon=True)
            thread.start()
        
        self.message_user(request, f"Marked {updated} leads as interested.")

    @admin.action(description="Mark as Converted")
    def mark_converted(self, request, queryset):
        # Get leads with GHL contact IDs before update (for syncing)
        leads_with_ghl = list(queryset.filter(ghl_contact_id__isnull=False).values_list('id', 'ghl_contact_id'))
        
        # Update status
        updated = queryset.update(
            status="converted",
            converted=True,
            converted_at=timezone.now()
        )
        
        # Sync status to GHL for leads that have contact IDs
        if leads_with_ghl:
            from ghl_integration.services import GoHighLevelService
            from threading import Thread
            
            def sync_statuses_in_background():
                service = GoHighLevelService()
                synced_count = 0
                for lead_id, contact_id in leads_with_ghl:
                    try:
                        success = service.update_contact_status_fields(
                            contact_id=contact_id,
                            status="converted"
                        )
                        if success:
                            synced_count += 1
                    except Exception as e:
                        logger.error(f"Failed to sync status to GHL for Lead #{lead_id}: {e}")
                
                logger.info(f"Synced status to GHL for {synced_count} out of {len(leads_with_ghl)} leads")
            
            thread = Thread(target=sync_statuses_in_background, daemon=True)
            thread.start()
        
        self.message_user(request, f"Marked {updated} leads as converted.")
    
    @admin.action(description="Check for Auto-Conversion")
    def check_auto_conversion(self, request, queryset):
        """
        Check if any of the selected leads should be auto-converted based on existing reservations.
        This is useful for leads that existed before the auto-conversion system was implemented.
        """
        from .models import Reservation
        
        converted_count = 0
        for lead in queryset:
            if lead.status != 'converted':
                # Check if there's a reservation with matching email or phone
                matching_reservation = None
                
                if lead.email:
                    matching_reservation = Reservation.objects.filter(
                        customer__email__iexact=lead.email
                    ).first()
                
                if not matching_reservation and lead.phone:
                    matching_reservation = Reservation.objects.filter(
                        customer__phone_number__iexact=lead.phone
                    ).first()
                
                if matching_reservation:
                    lead.status = 'converted'
                    lead.converted = True
                    lead.converted_at = timezone.now()
                    
                    # Add conversion note
                    conversion_note = f"Auto-converted on {timezone.now().strftime('%Y-%m-%d %H:%M')} - Found existing Reservation #{matching_reservation.id}"
                    if lead.notes:
                        lead.notes += f"\n\n{conversion_note}"
                    else:
                        lead.notes = conversion_note
                    
                    lead.save()
                    converted_count += 1
        
        if converted_count > 0:
            self.message_user(request, f"Auto-converted {converted_count} leads based on existing reservations.")
        else:
            self.message_user(request, "No leads were auto-converted. All selected leads are either already converted or don't have matching reservations.")

    @admin.action(description="Mark as Lost")
    def mark_lost(self, request, queryset):
        # Get leads with GHL contact IDs before update (for syncing)
        leads_with_ghl = list(queryset.filter(ghl_contact_id__isnull=False).values_list('id', 'ghl_contact_id'))
        
        # Update status
        updated = queryset.update(status="lost")
        
        # Sync status to GHL for leads that have contact IDs
        if leads_with_ghl:
            from ghl_integration.services import GoHighLevelService
            from threading import Thread
            
            def sync_statuses_in_background():
                service = GoHighLevelService()
                synced_count = 0
                for lead_id, contact_id in leads_with_ghl:
                    try:
                        success = service.update_contact_status_fields(
                            contact_id=contact_id,
                            status="lost"
                        )
                        if success:
                            synced_count += 1
                    except Exception as e:
                        logger.error(f"Failed to sync status to GHL for Lead #{lead_id}: {e}")
                
                logger.info(f"Synced status to GHL for {synced_count} out of {len(leads_with_ghl)} leads")
            
            thread = Thread(target=sync_statuses_in_background, daemon=True)
            thread.start()
        
        self.message_user(request, f"Marked {updated} leads as lost.")

    @admin.action(description="Set High Priority")
    def set_high_priority(self, request, queryset):
        queryset.update(priority="high")
        self.message_user(request, f"Set {queryset.count()} leads to high priority.")

    @admin.action(description="Set Low Priority")
    def set_low_priority(self, request, queryset):
        queryset.update(priority="low")
        self.message_user(request, f"Set {queryset.count()} leads to low priority.")

    @admin.action(description="Set Urgent Priority")
    def set_urgent_priority(self, request, queryset):
        queryset.update(priority="urgent")
        self.message_user(request, f"Set {queryset.count()} leads to urgent priority.")

    @admin.action(description="Follow-up Tomorrow")
    def schedule_follow_up_tomorrow(self, request, queryset):
        tomorrow = timezone.now() + timedelta(days=1)
        queryset.update(next_follow_up=tomorrow)
        self.message_user(request, f"Scheduled follow-up for {queryset.count()} leads for tomorrow.")

    @admin.action(description="Follow-up Next Week")
    def schedule_follow_up_week(self, request, queryset):
        next_week = timezone.now() + timedelta(days=7)
        queryset.update(next_follow_up=next_week)
        self.message_user(request, f"Scheduled follow-up for {queryset.count()} leads for next week.")

    @admin.action(description="Follow-up Next Month")
    def schedule_follow_up_month(self, request, queryset):
        next_month = timezone.now() + timedelta(days=30)
        queryset.update(next_follow_up=next_month)
        self.message_user(request, f"Scheduled follow-up for {queryset.count()} leads for next month.")

    @admin.action(description="Identify Duplicates")
    def identify_duplicates(self, request, queryset):
        # This would implement duplicate detection logic
        self.message_user(request, "Duplicate detection feature coming soon!")

    @admin.action(description="Export Leads to CSV")
    def export_leads_csv(self, request, queryset):
        # This would implement CSV export functionality
        self.message_user(request, f"CSV export feature coming soon! Would export {queryset.count()} leads.")

    @admin.action(description="📱 Send SMS to selected leads")
    def send_sms_to_selected(self, request, queryset):
        """
        Queue selected leads for SMS sending via GoHighLevel.
        Prevents duplicate SMS by:
        - Grouping leads by phone number
        - Sending to most expensive lead per phone (or newest if same/no price)
        - Skipping phones that received SMS in last 18 hours
        """
        from ghl_integration.tasks import sync_lead_to_ghl_and_send_sms
        from threading import Thread
        from datetime import timedelta
        from collections import defaultdict
        
        # Time window for preventing duplicate SMS (18 hours)
        SMS_COOLDOWN_HOURS = 18
        cutoff_time = timezone.now() - timedelta(hours=SMS_COOLDOWN_HOURS)
        
        # Statistics
        queued_count = 0
        processed_count = 0
        skipped_no_phone = 0
        skipped_already_sent = 0
        skipped_duplicate_phone = 0
        skipped_recent_sms = 0
        
        # Group leads by phone number
        leads_by_phone = defaultdict(list)
        leads_to_process = []
        
        for lead in queryset:
            # Skip if no phone number
            if not lead.phone:
                skipped_no_phone += 1
                continue
            
            # Skip if SMS already sent (individual check)
            if lead.initial_sms_sent:
                skipped_already_sent += 1
                continue
            
            # Normalize phone for grouping (remove spaces, dashes, etc.)
            normalized_phone = ''.join(filter(str.isdigit, lead.phone))
            if normalized_phone:
                leads_by_phone[normalized_phone].append(lead)
        
        # Process each phone number group
        for phone, phone_leads in leads_by_phone.items():
            # Check if this phone received SMS in last 18 hours (anywhere in database)
            # Use last 10 digits for US phone matching (handles different formats)
            recent_sms_check = False
            if len(phone) >= 10:
                last_10_digits = phone[-10:]
                # Get all leads that received SMS recently
                recent_leads = Lead.objects.filter(
                    initial_sms_sent=True,
                    initial_sms_sent_at__gte=cutoff_time
                ).exclude(phone__isnull=True).exclude(phone='')
                
                # Check if any recent lead has matching phone (normalize and compare last 10 digits)
                for recent_lead in recent_leads:
                    recent_phone_digits = ''.join(filter(str.isdigit, recent_lead.phone))
                    if recent_phone_digits and len(recent_phone_digits) >= 10:
                        if recent_phone_digits[-10:] == last_10_digits:
                            recent_sms_check = True
                            break
            
            if recent_sms_check:
                # Skip all leads with this phone (received SMS recently)
                skipped_recent_sms += len(phone_leads)
                continue
            
            # Pick ONE lead to send to: most expensive first, then newest if same/no price
            selected_lead = max(
                phone_leads,
                key=lambda l: (
                    l.estimated_price if l.estimated_price else 0,
                    l.created_at if l.created_at else timezone.now()
                )
            )
            
            # Add selected lead to process list
            leads_to_process.append(selected_lead)
            
            # Mark ALL leads with this phone as "contacted" (even if they didn't get SMS)
            # This ensures all quotes for the same person are marked as contacted
            leads_to_mark_contacted = []
            for lead in phone_leads:
                if lead.status != 'contacted':
                    lead.status = 'contacted'
                    lead.contact_attempts = (lead.contact_attempts or 0) + 1
                    lead.last_contact_date = timezone.now()
                    lead.save(update_fields=['status', 'contact_attempts', 'last_contact_date'])
                    # Track leads that need GHL status sync
                    if lead.ghl_contact_id:
                        leads_to_mark_contacted.append(lead.ghl_contact_id)
            
            # Sync status to GHL for all leads with this phone (in background)
            if leads_to_mark_contacted:
                from ghl_integration.services import GoHighLevelService
                from threading import Thread
                
                def sync_all_contacted_statuses():
                    service = GoHighLevelService()
                    for contact_id in leads_to_mark_contacted:
                        try:
                            service.update_contact_status_fields(
                                contact_id=contact_id,
                                status="contacted"
                            )
                        except Exception as e:
                            logger.error(f"Failed to sync contacted status to GHL for contact {contact_id}: {e}")
                
                thread = Thread(target=sync_all_contacted_statuses, daemon=True)
                thread.start()
            
            # Count skipped duplicates (for SMS sending, not for status update)
            if len(phone_leads) > 1:
                skipped_duplicate_phone += len(phone_leads) - 1
        
        # Process selected leads
        for lead in leads_to_process:
            # Try to queue with Celery, fallback to thread if Celery unavailable
            try:
                sync_lead_to_ghl_and_send_sms.delay(lead.id)
                queued_count += 1
            except Exception as e:
                # Celery not available, run in background thread
                logger.warning(f"Could not queue Celery task for lead {lead.id}: {e}. Running in thread instead.")
                def run_task(lead_id=lead.id):
                    try:
                        sync_lead_to_ghl_and_send_sms(lead_id)
                    except Exception as task_error:
                        logger.error(f"Error processing lead {lead_id} in thread: {task_error}")
                
                thread = Thread(target=run_task, daemon=True)
                thread.start()
                processed_count += 1
        
        # Build success message
        message_parts = []
        total_processed = queued_count + processed_count
        if total_processed > 0:
            if queued_count > 0:
                message_parts.append(f"Queued {queued_count} lead{'s' if queued_count != 1 else ''} for SMS sending")
            if processed_count > 0:
                message_parts.append(f"Processing {processed_count} lead{'s' if processed_count != 1 else ''} in background")
        if skipped_no_phone > 0:
            message_parts.append(f"{skipped_no_phone} skipped (no phone number)")
        if skipped_already_sent > 0:
            message_parts.append(f"{skipped_already_sent} skipped (SMS already sent)")
        if skipped_duplicate_phone > 0:
            message_parts.append(f"{skipped_duplicate_phone} skipped (duplicate phone)")
        if skipped_recent_sms > 0:
            message_parts.append(f"{skipped_recent_sms} skipped (SMS sent in last {SMS_COOLDOWN_HOURS} hours)")
        
        if total_processed > 0:
            self.message_user(request, ". ".join(message_parts) + ".", messages.SUCCESS)
        else:
            self.message_user(request, "No leads were queued. " + ". ".join(message_parts) + ".", messages.WARNING)

    @admin.action(description="🔄 Sync to GHL without SMS")
    def sync_to_ghl_without_sms(self, request, queryset):
        """
        Sync selected leads to GoHighLevel without sending SMS.
        Useful for importing existing leads or syncing contact information.
        Skips leads without phone numbers.
        """
        from ghl_integration.tasks import sync_lead_to_ghl_without_sms
        from threading import Thread
        
        queued_count = 0
        processed_count = 0
        skipped_no_phone = 0
        
        for lead in queryset:
            # Skip if no phone number
            if not lead.phone:
                skipped_no_phone += 1
                continue
            
            # Try to queue with Celery, fallback to thread if Celery unavailable
            try:
                sync_lead_to_ghl_without_sms.delay(lead.id)
                queued_count += 1
            except Exception as e:
                # Celery not available, run in background thread
                logger.warning(f"Could not queue Celery task for lead {lead.id}: {e}. Running in thread instead.")
                def run_task():
                    try:
                        sync_lead_to_ghl_without_sms(lead.id)
                    except Exception as task_error:
                        logger.error(f"Error syncing lead {lead.id} in thread: {task_error}")
                
                thread = Thread(target=run_task, daemon=True)
                thread.start()
                processed_count += 1
        
        # Build success message
        message_parts = []
        total_processed = queued_count + processed_count
        if total_processed > 0:
            if queued_count > 0:
                message_parts.append(f"Queued {queued_count} lead{'s' if queued_count != 1 else ''} for GHL sync")
            if processed_count > 0:
                message_parts.append(f"Processing {processed_count} lead{'s' if processed_count != 1 else ''} in background")
        if skipped_no_phone > 0:
            message_parts.append(f"{skipped_no_phone} skipped (no phone number)")
        
        if total_processed > 0:
            self.message_user(request, ". ".join(message_parts) + ".", messages.SUCCESS)
        else:
            self.message_user(request, "No leads were queued. " + ". ".join(message_parts) + ".", messages.WARNING)



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
