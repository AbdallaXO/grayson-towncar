from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.db.models import Sum, F, Q, Count, Case, When, Value, DecimalField, Subquery, OuterRef
from django.db.models.functions import Coalesce
from django.utils.safestring import mark_safe
from .models import Driver, DriverPayment, LegPayment, FleetVehicle, DriverWeeklySchedule, DriverPayRate, DriverDateOverride, DriverPaymentExport, DriverPayoutAdjustment, AffiliateProfile
from reservations.models import Leg
from decimal import Decimal
from dispatching.admin_mixins import DispatcherAdminMixin


class DriverWeeklyScheduleInline(admin.TabularInline):
    model = DriverWeeklySchedule
    extra = 0
    max_num = 7
    fields = ["day_of_week", "is_available", "shift_type", "start_hour", "end_hour", "flexible", "max_hours", "preferred_shift", "preference", "scheduling_notes"]


class DriverDateOverrideInline(admin.TabularInline):
    model = DriverDateOverride
    extra = 1
    fields = ["date", "end_date", "exception_type", "start_time", "end_time", "reason", "notes"]
    ordering = ["date"]
    fk_name = "driver"


class DriverPayRateInline(admin.TabularInline):
    model = DriverPayRate
    extra = 1
    fields = ["route", "vehicle", "direction", "base_pay"]


@admin.register(Driver)
class DriverAdmin(DispatcherAdminMixin, admin.ModelAdmin):
    show_full_result_count = False  # Prevents Jazzmin from running a second COUNT(*) over the full annotated queryset
    list_display = [
        "driver_name",
        "active_status",
        "email",
        "phone_number",
        "driver_type_display",
        "vehicle",
        "unpaid_legs_count",
        "unpaid_amount",
        "total_paid",
        "total_legs",
        "profit_performance",
    ]
    list_filter = [
        "is_active",
        "profile__is_active",
        "payment_method",
        "driver_type",
    ]
    search_fields = [
        "profile__username",
        "profile__first_name",
        "profile__last_name",
        "profile__email",
        "phone_number",
        "vehicle",
    ]

    inlines = [DriverPayRateInline, DriverWeeklyScheduleInline, DriverDateOverrideInline]

    fieldsets = (
        (
            "Driver Information",
            {
                "fields": (
                    "profile",
                    "is_active",
                    "driver_type",
                    "phone_number",
                    "vehicle",
                    "schedule",
                    "notes",
                    "payment_method",
                    "night_bonus",
                ),
                "description": "Uncheck \"Is active\" to hide a driver from the dispatcher directory "
                               "(e.g. when they leave the company or go on extended leave). "
                               "Historical legs and payments stay intact.",
            },
        ),
        (
            "Gusto Payroll (optional)",
            {
                "fields": (
                    "gusto_first_name",
                    "gusto_last_name",
                    "gusto_business_name",
                    "gusto_ssn_ein_last4",
                    "gusto_contractor_id",
                    "gusto_payment_type",
                ),
                "description": "Optional fields used only by the Gusto Smart Import CSV export. "
                               "Leave first/last blank to fall back to the user's profile name. "
                               "Store ONLY the last 4 digits of SSN/EIN (e.g. \"9579\" or \"*9579\") — never the full number.",
                "classes": ("collapse",),
            },
        ),
        (
            "Auto-Assign Defaults",
            {
                "fields": (
                    "default_shift_type",
                    "default_start_hour",
                    "default_end_hour",
                    "default_flexible",
                    "default_max_hours",
                    "default_preferred_shift",
                    "default_preference",
                ),
                "description": "Default working hours and preferences for auto-assign. "
                               "Flexible = no hard time limits, planner builds a reasonable shift. "
                               "Uncheck Flexible only if driver has strict start/end constraints. "
                               "Per-day overrides can be set in the Weekly Schedule section below.",
            },
        ),
        (
            "Payment Tracking",
            {
                "fields": (
                    "unpaid_legs_display",
                    "unpaid_amount_display",
                    "recent_leg_history",
                    "profit_summary",
                ),
                "description": "Payment status, leg history, and profit information.",
            },
        ),
    )
    readonly_fields = [
        "unpaid_legs_display",
        "unpaid_amount_display",
        "recent_leg_history",
        "profit_summary",
    ]

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        # _total_paid joins on DriverPayment (different table from legs).
        # Keeping it as a Subquery avoids a cartesian-product fan-out when legs is also JOINed below.
        total_paid_subq = (
            DriverPayment.objects.filter(driver=OuterRef('pk'))
            .values('driver')
            .annotate(total=Sum('amount'))
            .values('total')
        )

        # All leg-based metrics share a single LEFT JOIN on legs — no fan-out between them.
        return qs.select_related('profile').annotate(
            _unpaid_legs_count=Count('legs', filter=Q(legs__payment_status='unpaid'), distinct=True),
            _total_legs_count=Count('legs', distinct=True),
            # Conditional SUM replaces the correlated unpaid_amount_subq (per-row subquery → GROUP BY)
            _unpaid_amount=Coalesce(
                Sum(
                    Case(
                        When(
                            legs__payment_status='unpaid',
                            legs__driver_base_pay__isnull=False,
                            then=(
                                Coalesce(F('legs__driver_base_pay'), Value(Decimal('0.00')))
                                + Coalesce(F('legs__driver_gratuity'), Value(Decimal('0.00')))
                                + Coalesce(F('legs__driver_additional'), Value(Decimal('0.00')))
                            ),
                        ),
                        When(
                            legs__payment_status='unpaid',
                            then=Coalesce(F('legs__driver_pay_amount'), Value(Decimal('0.00'))),
                        ),
                        default=Value(Decimal('0.00')),
                        output_field=DecimalField(max_digits=10, decimal_places=2),
                    ),
                    filter=Q(legs__payment_status='unpaid'),
                ),
                Value(Decimal('0.00')),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ),
            # SUM replaces the correlated total_profit_subq
            _total_profit=Coalesce(
                Sum('legs__profit_estimate', filter=Q(legs__profit_estimate__isnull=False)),
                Value(Decimal('0.00')),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ),
            _total_revenue=Coalesce(
                Sum('legs__revenue_share', filter=Q(legs__revenue_share__isnull=False)),
                Value(Decimal('0.00')),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ),
            # DriverPayment kept as Subquery — joining both legs and driverpayment in one query
            # would create a cartesian product that inflates all sums.
            _total_paid=Coalesce(
                Subquery(total_paid_subq, output_field=DecimalField(max_digits=10, decimal_places=2)),
                Value(Decimal('0.00')),
            ),
        )

    def driver_name(self, obj):
        return (
            f"{obj.profile.first_name} {obj.profile.last_name}"
            if obj.profile.first_name
            else obj.profile.username
        )

    driver_name.short_description = "Name"

    def active_status(self, obj):
        if obj.is_active:
            return format_html('<span style="color: #198754; font-weight: bold;">Active</span>')
        return format_html('<span style="color: #6c757d; font-weight: bold;">Inactive</span>')

    active_status.short_description = "Active"
    active_status.admin_order_field = "is_active"

    def email(self, obj):
        return obj.profile.email

    email.short_description = "Email"

    def driver_type_display(self, obj):
        if obj.driver_type == "inhouse":
            return format_html('<span style="color: #0d6efd; font-weight: bold;">Inhouse</span>')
        else:
            return format_html('<span style="color: #ffc107; font-weight: bold;">Affiliate</span>')

    driver_type_display.short_description = "Type"

    def unpaid_legs_count(self, obj):
        count = obj._unpaid_legs_count
        if count > 0:
            url = (
                reverse("admin:reservations_leg_changelist")
                + f"?driver__id__exact={obj.id}&payment_status__exact=unpaid"
            )
            return format_html(
                '<a href="{}" style="color: red; font-weight: bold;">{}</a>', url, count
            )
        return format_html('<span style="color: green;">0</span>')

    unpaid_legs_count.short_description = "Unpaid Legs"

    def unpaid_amount(self, obj):
        amount = obj._unpaid_amount or Decimal('0.00')
        if amount > 0:
            return format_html('<span style="color: green;">${0}</span>', amount)
        elif amount < 0:
            return format_html('<span style="color: red;">${0}</span>', abs(amount))
        return format_html('<span style="color: green;">$0.00</span>')

    unpaid_amount.short_description = "Unpaid Amount"

    def unpaid_legs_display(self, obj):
        legs = obj.get_unpaid_legs()
        if not legs:
            return "No unpaid legs"

        html = '<table style="width: 100%; border-collapse: collapse;">'
        html += '<tr style="background-color: #f0f0f0;"><th>Date</th><th>Route</th><th>Amount</th><th>Profit</th><th>Status</th></tr>'

        for leg in legs:
            html += "<tr>"
            html += f'<td><a href="{reverse("admin:reservations_leg_change", args=[leg.id])}">{leg.pickup_date}</a></td>'
            html += f"<td>{leg.pickup_location} to {leg.dropoff_location}</td>"

            # Get driver pay amount
            amount = leg.driver_pay_amount or 0
            profit = leg.profit_estimate or 0

            # Format amount with proper color
            if amount >= 0:
                amount_html = f'<span style="color: green;">${amount}</span>'
            else:
                amount_html = f'<span style="color: red;">${abs(amount)}</span>'

            # Format profit with proper color
            if profit >= 0:
                profit_html = f'<span style="color: green;">${profit}</span>'
            else:
                profit_html = f'<span style="color: red;">${abs(profit)}</span>'

            html += f"<td>{amount_html}</td>"
            html += f"<td>{profit_html}</td>"

            # Show leg status in addition to payment status
            html += f'<td><span style="color: red;">Unpaid</span> ({leg.status})</td>'
            html += "</tr>"

        html += "</table>"
        return mark_safe(html)

    unpaid_legs_display.short_description = "Unpaid Legs"

    def unpaid_amount_display(self, obj):
        amount = obj.get_total_unpaid_amount()
        if amount >= 0:
            return f"${amount}"
        else:
            return f"-${abs(amount)}"

    unpaid_amount_display.short_description = "Unpaid Amount"

    def total_paid(self, obj):
        total_amount = obj._total_paid
        if total_amount > 0:
            return format_html('<span style="color: green;">${0}</span>', total_amount)
        elif total_amount < 0:
            return format_html(
                '<span style="color: red;">${0}</span>', abs(total_amount)
            )
        else:
            return format_html('<span style="color: green;">$0.00</span>')

    total_paid.short_description = "Total Paid"

    def total_legs(self, obj):
        count = obj._total_legs_count
        url = (
            reverse("admin:reservations_leg_changelist")
            + f"?driver__id__exact={obj.id}"
        )
        return format_html('<a href="{}">{}</a>', url, count)

    total_legs.short_description = "Total Legs"

    def profit_performance(self, obj):
        """Display driver's profit performance based on their assigned legs"""
        total_profit = obj._total_profit or Decimal('0.00')
        total_revenue = obj._total_revenue or Decimal('0.00')
        if obj._total_legs_count == 0:
            return "N/A"

        margin_str = ""
        if total_revenue > 0:
            margin = (total_profit / total_revenue) * 100
            margin_str = f" ({margin:.1f}%)"

        if total_profit >= 0:
            return format_html('<span style="color: green;">${0}{1}</span>', total_profit, margin_str)
        else:
            return format_html(
                '<span style="color: red;">-${0}{1}</span>', abs(total_profit), margin_str
            )

    profit_performance.short_description = "Profit (Margin)"

    def profit_summary(self, obj):
        """Detailed profit information for this driver — single aggregate query."""
        legs_qs = obj.legs.all()

        # One query: overall counts + sums + per-status breakdown
        agg = legs_qs.aggregate(
            total_legs=Count("id"),
            completed_legs=Count(Case(When(status="completed", then=1))),
            total_revenue=Coalesce(Sum("revenue_share"), Value(Decimal("0.00"))),
            total_driver_pay=Coalesce(
                Sum(
                    Case(
                        When(
                            Q(driver_base_pay__isnull=False)
                            | Q(driver_gratuity__isnull=False)
                            | Q(driver_additional__isnull=False),
                            then=(
                                Coalesce(F("driver_base_pay"), Value(Decimal("0.00")))
                                + Coalesce(F("driver_gratuity"), Value(Decimal("0.00")))
                                + Coalesce(F("driver_additional"), Value(Decimal("0.00")))
                            ),
                        ),
                        default=Coalesce(F("driver_pay_amount"), Value(Decimal("0.00"))),
                        output_field=DecimalField(),
                    )
                ),
                Value(Decimal("0.00")),
            ),
            total_profit=Coalesce(Sum("profit_estimate"), Value(Decimal("0.00"))),
        )

        total_legs = agg["total_legs"]
        if not total_legs:
            return "No legs assigned to this driver"

        completed_legs = agg["completed_legs"]
        total_revenue = agg["total_revenue"]
        total_driver_pay = agg["total_driver_pay"]
        total_profit = agg["total_profit"]
        avg_profit_per_leg = total_profit / total_legs

        # Per-status breakdown — one query with conditional aggregation
        status_choices = [c[0] for c in Leg._meta.get_field("status").choices]
        status_agg_kwargs = {}
        for s in status_choices:
            safe = s.replace("-", "_")
            status_agg_kwargs[f"cnt_{safe}"] = Count(Case(When(status=s, then=1)))
            status_agg_kwargs[f"profit_{safe}"] = Coalesce(
                Sum(Case(When(status=s, then=F("profit_estimate")))),
                Value(Decimal("0.00")),
            )
        status_data = legs_qs.aggregate(**status_agg_kwargs)

        status_profits = {}
        for s in status_choices:
            safe = s.replace("-", "_")
            cnt = status_data[f"cnt_{safe}"]
            if cnt:
                status_profits[s] = (cnt, status_data[f"profit_{safe}"])

        # Build HTML
        def _color(val):
            if val >= 0:
                return f'<span style="color: green;">${val}</span>'
            return f'<span style="color: red;">${abs(val)}</span>'

        html = "<h3>Profit Summary</h3>"
        html += '<table style="width: 100%; border-collapse: collapse;">'
        html += '<tr style="background-color: #f0f0f0;"><th colspan="2">Overall Statistics</th></tr>'
        html += f"<tr><td>Total Legs:</td><td>{total_legs}</td></tr>"
        html += f"<tr><td>Completed Legs:</td><td>{completed_legs}</td></tr>"
        html += f"<tr><td>Total Revenue Share:</td><td>${total_revenue}</td></tr>"
        html += f"<tr><td>Total Driver Pay:</td><td>{_color(total_driver_pay)}</td></tr>"
        html += f"<tr><td>Total Profit:</td><td>{_color(total_profit)}</td></tr>"
        html += f"<tr><td>Average Profit per Leg:</td><td>{_color(avg_profit_per_leg)}</td></tr>"

        if status_profits:
            html += '<tr style="background-color: #f0f0f0;"><th colspan="2">Profit by Status</th></tr>'
            for status, (count, profit) in status_profits.items():
                status_display = status.title() if status else "Unknown"
                html += f"<tr><td>{status_display} ({count}):</td><td>{_color(profit)}</td></tr>"

        html += "</table>"
        return mark_safe(html)

    profit_summary.short_description = "Profit Summary"

    def recent_leg_history(self, obj):
        legs = obj.get_leg_history()[:10]
        if not legs:
            return "No legs found"

        html = '<table style="width: 100%; border-collapse: collapse;">'
        html += '<tr style="background-color: #f0f0f0;"><th>Date</th><th>Route</th><th>Amount</th><th>Profit</th><th>Status</th></tr>'

        for leg in legs:
            html += "<tr>"
            html += f'<td><a href="{reverse("admin:reservations_leg_change", args=[leg.id])}">{leg.pickup_date}</a></td>'
            html += f"<td>{leg.pickup_location} to {leg.dropoff_location}</td>"

            # Get driver pay amount (use new property that handles base_pay + gratuity)
            amount = leg.total_driver_pay
            profit = leg.profit_estimate or 0

            # Format amount with color based on value
            if amount >= 0:
                amount_html = f'<span style="color: green;">${amount}</span>'
            else:
                amount_html = f'<span style="color: red;">${abs(amount)}</span>'

            # Format profit with color based on value
            if profit >= 0:
                profit_html = f'<span style="color: green;">${profit}</span>'
            else:
                profit_html = f'<span style="color: red;">${abs(profit)}</span>'

            html += f"<td>{amount_html}</td>"
            html += f"<td>{profit_html}</td>"

            # Payment status
            if leg.payment_status == "paid":
                status_html = '<span style="color: green;">Paid</span>'
            elif leg.payment_status == "unpaid":
                status_html = '<span style="color: red;">Unpaid</span>'
            else:
                status_html = leg.payment_status.title()

            html += f"<td>{status_html}</td>"
            html += "</tr>"

        html += "</table>"
        html += f'<br><a href="{reverse("admin:reservations_leg_changelist")}?driver__id__exact={obj.id}">View all legs</a>'

        return mark_safe(html)

    recent_leg_history.short_description = "Recent Leg History"

    actions = ["preview_driver_payments", "process_driver_payments"]

    def process_driver_payments(self, request, queryset):
        """Process payments for all unpaid COMPLETED legs for selected drivers."""
        from django.contrib import messages
        from django.utils import timezone
        import logging

        logger = logging.getLogger(__name__)
        processed_count = 0
        total_amount = 0

        for driver in queryset:
            # Get unpaid COMPLETED legs only
            unpaid_legs = driver.get_unpaid_legs().filter(status="completed")

            # Log the number of unpaid completed legs found
            logger.info(
                f"Driver {driver} has {unpaid_legs.count()} unpaid completed legs"
            )

            if unpaid_legs:
                # Group legs by reservation
                reservation_legs = {}
                for leg in unpaid_legs:
                    if leg.reservation:
                        if leg.reservation not in reservation_legs:
                            reservation_legs[leg.reservation] = []
                        reservation_legs[leg.reservation].append(leg)

                # Calculate total
                payment_total = sum(leg.total_driver_pay for leg in unpaid_legs)

                # Create simplified notes for driver communication
                notes = []
                notes.append(f"Payment Summary for {driver.profile.get_full_name()}")
                notes.append(f"Payment Date: {timezone.now().strftime('%B %d, %Y')}")
                notes.append(f"Total Legs: {unpaid_legs.count()}")
                notes.append("\nReservation Details:")
                notes.append("-" * 50)

                for reservation, legs in reservation_legs.items():
                    leg_total = sum(leg.total_driver_pay for leg in legs)

                    # Reservation header
                    notes.append(
                        f"\nReservation #{reservation.id} - {reservation.customer.get_full_name()}"
                    )

                    # Leg details (simplified)
                    for leg in legs:
                        notes.append(
                            f"  • {leg.pickup_date.strftime('%m/%d/%Y')} | "
                            f"{leg.pickup_location} → {leg.dropoff_location} | "
                            f"Payment: ${leg.total_driver_pay:.2f}"
                        )

                    # Subtotal for this reservation
                    if len(legs) > 1:
                        notes.append(f"  Subtotal: ${leg_total:.2f}")

                # Add summary at the end
                notes.append("\n" + "-" * 50)
                notes.append(f"TOTAL PAYMENT: ${payment_total:.2f}")
                notes.append(
                    f"Payment Method: {driver.payment_method or 'Direct Deposit'}"
                )
                notes.append(f"Reference: Auto-{timezone.now().strftime('%Y%m%d')}")

                try:
                    # Create payment record using the class method
                    logger.info(
                        f"Creating payment for {driver} with {unpaid_legs.count()} completed legs"
                    )

                    payment = DriverPayment.create_payment(
                        driver=driver,
                        legs=unpaid_legs,
                        payment_method=driver.payment_method or "direct deposit",
                        reference_number=f"Auto-{timezone.now().strftime('%Y%m%d')}",
                        notes="\n".join(notes),
                        created_by=request.user,
                    )

                    # Verify leg payments were created
                    leg_payment_count = payment.leg_payments.count()
                    logger.info(
                        f"Payment created with ID {payment.id}, with {leg_payment_count} leg payments"
                    )

                    processed_count += 1
                    total_amount += payment_total

                except Exception as e:
                    logger.error(
                        f"Error processing payment for {driver}: {e}", exc_info=True
                    )
                    messages.error(request, f"Error processing {driver}: {e}")

        if processed_count:
            messages.success(
                request,
                f"Processed payments for {processed_count} drivers. Total: ${total_amount:.2f}",
            )
        else:
            messages.info(
                request, "No unpaid completed legs found for selected drivers."
            )

    process_driver_payments.short_description = (
        "Process driver payments (completed legs only)"
    )

    def preview_driver_payments(self, request, queryset):
        """Show a preview of driver payments without actually processing them."""
        from django.contrib import messages
        from collections import defaultdict

        # Batch-fetch all unpaid completed legs for the selected drivers in ONE
        # query, grouped by driver, instead of driver.get_unpaid_legs() per driver
        # (N+1). Same filter as driver.get_unpaid_legs().filter(status="completed").
        legs_by_driver = defaultdict(list)
        for leg in Leg.objects.filter(
            driver_id__in=[d.id for d in queryset],
            payment_status="unpaid",
            status="completed",
        ):
            legs_by_driver[leg.driver_id].append(leg)

        preview_data = []
        total_amount = 0

        for driver in queryset:
            # Unpaid COMPLETED legs only (from the prefetched batch)
            unpaid_legs = legs_by_driver.get(driver.id, [])

            if unpaid_legs:
                # Calculate total
                payment_total = sum(leg.total_driver_pay for leg in unpaid_legs)

                # Count legs
                leg_count = len(unpaid_legs)

                # Get date range
                leg_dates = [leg.pickup_date for leg in unpaid_legs if leg.pickup_date]
                if leg_dates:  # Check if there are actually dates to get min/max from
                    start_date = min(leg_dates)
                    end_date = max(leg_dates)

                    # Add to preview data
                    preview_data.append(
                        {
                            "driver": str(driver),
                            "amount": payment_total,
                            "count": leg_count,
                            "period": f"{start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}",
                        }
                    )

                    total_amount += payment_total

        if preview_data:
            # Create a message with the preview data
            message = "Driver Payment Preview (Completed Legs Only):<br><br>"
            message += "<table style='border-collapse: collapse; width: 100%;'>"
            message += "<tr style='background-color: #f2f2f2;'><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Driver</th><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Completed Legs</th><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Period</th><th style='padding: 8px; text-align: right; border: 1px solid #ddd;'>Amount</th></tr>"

            for item in preview_data:
                # Format amounts with color
                amount_color = "green" if item["amount"] >= 0 else "red"

                message += f"""<tr>
                    <td style='padding: 8px; border: 1px solid #ddd;'>{item["driver"]}</td>
                    <td style='padding: 8px; border: 1px solid #ddd;'>{item["count"]}</td>
                    <td style='padding: 8px; border: 1px solid #ddd;'>{item["period"]}</td>
                    <td style='padding: 8px; text-align: right; border: 1px solid #ddd;'>
                        <span style='color: {amount_color};'>${item["amount"]:.2f}</span>
                    </td>
                </tr>"""

            # Format total with color
            total_amount_color = "green" if total_amount >= 0 else "red"

            message += f"""<tr style='background-color: #f2f2f2;'>
                <td colspan='3' style='padding: 8px; border: 1px solid #ddd; text-align: right;'>
                    <strong>Total:</strong>
                </td>
                <td style='padding: 8px; border: 1px solid #ddd; text-align: right;'>
                    <strong><span style='color: {total_amount_color};'>${total_amount:.2f}</span></strong>
                </td>
            </tr>"""

            message += "</table><br>"
            message += "To process these payments, select the drivers again and use the 'Process driver payments (completed legs only)' action."

            self.message_user(request, mark_safe(message))
        else:
            self.message_user(
                request, "No unpaid completed legs found for the selected drivers."
            )

    preview_driver_payments.short_description = (
        "Preview driver payments (completed legs only)"
    )

    def process_driver_payments(self, request, queryset):
        """Process payments for all unpaid COMPLETED legs for selected drivers."""
        from django.contrib import messages
        from django.utils import timezone
        import logging

        logger = logging.getLogger(__name__)
        processed_count = 0
        total_amount = 0

        for driver in queryset:
            # Get unpaid COMPLETED legs only
            unpaid_legs = driver.get_unpaid_legs().filter(status="completed")

            # Log the number of unpaid completed legs found
            logger.info(
                f"Driver {driver} has {unpaid_legs.count()} unpaid completed legs"
            )

            if unpaid_legs:
                # Group legs by reservation
                reservation_legs = {}
                for leg in unpaid_legs:
                    if leg.reservation:
                        if leg.reservation not in reservation_legs:
                            reservation_legs[leg.reservation] = []
                        reservation_legs[leg.reservation].append(leg)

                # Calculate total
                payment_total = sum(leg.total_driver_pay for leg in unpaid_legs)

                # Create simplified notes for driver communication
                notes = []
                notes.append(f"Payment Summary for {driver.profile.get_full_name()}")
                notes.append(f"Payment Date: {timezone.now().strftime('%B %d, %Y')}")
                notes.append(f"Total Legs: {unpaid_legs.count()}")
                notes.append("\nReservation Details:")
                notes.append("-" * 50)

                for reservation, legs in reservation_legs.items():
                    leg_total = sum(leg.total_driver_pay for leg in legs)

                    # Reservation header
                    notes.append(
                        f"\nReservation #{reservation.id} - {reservation.customer.get_full_name()}"
                    )

                    # Leg details (simplified)
                    for leg in legs:
                        notes.append(
                            f"  • {leg.pickup_date.strftime('%m/%d/%Y')} | "
                            f"{leg.pickup_location} → {leg.dropoff_location} | "
                            f"Payment: ${leg.total_driver_pay:.2f}"
                        )

                    # Subtotal for this reservation
                    if len(legs) > 1:
                        notes.append(f"  Subtotal: ${leg_total:.2f}")

                # Add summary at the end
                notes.append("\n" + "-" * 50)
                notes.append(f"TOTAL PAYMENT: ${payment_total:.2f}")
                notes.append(
                    f"Payment Method: {driver.payment_method or 'Direct Deposit'}"
                )
                notes.append(f"Reference: Auto-{timezone.now().strftime('%Y%m%d')}")

                try:
                    # Create payment record using the class method
                    logger.info(
                        f"Creating payment for {driver} with {unpaid_legs.count()} completed legs"
                    )

                    payment = DriverPayment.create_payment(
                        driver=driver,
                        legs=unpaid_legs,
                        payment_method=driver.payment_method or "direct deposit",
                        reference_number=f"Auto-{timezone.now().strftime('%Y%m%d')}",
                        notes="\n".join(notes),
                        created_by=request.user,
                    )

                    # Verify leg payments were created
                    leg_payment_count = payment.leg_payments.count()
                    logger.info(
                        f"Payment created with ID {payment.id}, with {leg_payment_count} leg payments"
                    )

                    processed_count += 1
                    total_amount += payment_total

                except Exception as e:
                    logger.error(
                        f"Error processing payment for {driver}: {e}", exc_info=True
                    )
                    messages.error(request, f"Error processing {driver}: {e}")

        if processed_count:
            messages.success(
                request,
                f"Processed payments for {processed_count} drivers. Total: ${total_amount:.2f}",
            )
        else:
            messages.info(
                request, "No unpaid completed legs found for selected drivers."
            )

    process_driver_payments.short_description = (
        "Process driver payments (completed legs only)"
    )


@admin.register(DriverPayment)
class DriverPaymentAdmin(admin.ModelAdmin):
    show_full_result_count = False
    list_display = [
        "id",
        "driver_link",
        "base_pay_display",
        "gratuity_display",
        "additional_display",
        "amount_display",
        "payment_date",
        "payment_method",
        "leg_count",
    ]
    list_filter = ["payment_method", "payment_date"]
    search_fields = [
        "driver__profile__first_name",
        "driver__profile__last_name",
        "reference_number",
        "notes",
    ]
    date_hierarchy = "payment_date"

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related(
            'driver', 'driver__profile'
        ).annotate(
            _leg_count=Count('leg_payments'),
        )

    def driver_link(self, obj):
        url = reverse("admin:drivers_driver_change", args=[obj.driver.id])
        return format_html('<a href="{}">{}</a>', url, obj.driver)

    driver_link.short_description = "Driver"

    def base_pay_display(self, obj):
        if obj.base_pay is not None:
            return format_html('<span style="color: blue;">${0}</span>', obj.base_pay)
        return format_html('<span style="color: gray;">-</span>')
    
    base_pay_display.short_description = "Base Pay"

    def gratuity_display(self, obj):
        if obj.gratuity is not None:
            return format_html('<span style="color: orange;">${0}</span>', obj.gratuity)
        return format_html('<span style="color: gray;">-</span>')
    
    gratuity_display.short_description = "Gratuity"

    def additional_display(self, obj):
        if obj.additional is not None:
            return format_html('<span style="color: purple;">${0}</span>', obj.additional)
        return format_html('<span style="color: gray;">-</span>')
    
    additional_display.short_description = "Additional"

    def amount_display(self, obj):
        # Format with color based on value
        if obj.amount >= 0:
            return format_html('<span style="color: green;">${0}</span>', obj.amount)
        else:
            return format_html('<span style="color: red;">${0}</span>', abs(obj.amount))

    amount_display.short_description = "Total"

    def leg_count(self, obj):
        return obj._leg_count

    leg_count.short_description = "Legs Paid"

    readonly_fields = ["payment_date", "leg_details"]

    fieldsets = (
        (
            "Payment Information",
            {
                "fields": (
                    "driver",
                    "base_pay",
                    "gratuity",
                    "additional",
                    "amount",
                    "payment_date",
                    "payment_method",
                    "reference_number",
                )
            },
        ),
        ("Notes", {"fields": ("notes",)}),
        ("Legs Paid", {"fields": ("leg_details",)}),
    )

    def leg_details(self, obj):
        leg_payments = obj.leg_payments.all().select_related("leg")
        if not leg_payments:
            return "No legs in this payment"

        html = '<table style="width: 100%; border-collapse: collapse;">'
        html += '<tr style="background-color: #f0f0f0;"><th>Leg ID</th><th>Date</th><th>Route</th><th>Amount</th><th>Profit</th></tr>'

        for lp in leg_payments:
            leg = lp.leg
            html += f"<tr>"
            html += f'<td><a href="{reverse("admin:reservations_leg_change", args=[leg.id])}">{leg.id}</a></td>'
            html += f"<td>{leg.pickup_date}</td>"
            html += f"<td>{leg.pickup_location} to {leg.dropoff_location}</td>"

            # Format amount with color
            if lp.amount >= 0:
                amount_html = f'<span style="color: green;">${lp.amount}</span>'
            else:
                amount_html = f'<span style="color: red;">${abs(lp.amount)}</span>'

            html += f"<td>{amount_html}</td>"

            # Add profit info if available
            profit = leg.profit_estimate or 0
            if profit >= 0:
                profit_html = f'<span style="color: green;">${profit}</span>'
            else:
                profit_html = f'<span style="color: red;">${abs(profit)}</span>'

            html += f"<td>{profit_html}</td>"
            html += f"</tr>"

        html += "</table>"
        return mark_safe(html)

    leg_details.short_description = "Leg Details"


@admin.register(LegPayment)
class LegPaymentAdmin(admin.ModelAdmin):
    list_display = ["id", "payment", "leg_display", "base_pay_display", "gratuity_display", "additional_display", "amount_display", "profit_display"]
    search_fields = [
        "payment__driver__profile__first_name",
        "payment__driver__profile__last_name",
        "leg__pickup_location",
        "leg__dropoff_location",
    ]
    list_filter = ["payment__payment_method", "payment__payment_date"]
    # Each changelist row renders the `payment` FK (str -> driver), `leg_display`
    # and `profit_display` (obj.leg fields). Without this, that's 2 queries/row (N+1).
    list_select_related = ("payment", "payment__driver", "payment__driver__profile", "leg")

    def leg_display(self, obj):
        url = reverse("admin:reservations_leg_change", args=[obj.leg.id])
        return format_html(
            '<a href="{}">{} - {}</a>', url, obj.leg.id, obj.leg.pickup_date
        )

    leg_display.short_description = "Leg"

    def base_pay_display(self, obj):
        if obj.base_pay is not None:
            return format_html('<span style="color: blue;">${0}</span>', obj.base_pay)
        return format_html('<span style="color: gray;">-</span>')
    
    base_pay_display.short_description = "Base Pay"

    def gratuity_display(self, obj):
        if obj.gratuity is not None:
            return format_html('<span style="color: orange;">${0}</span>', obj.gratuity)
        return format_html('<span style="color: gray;">-</span>')
    
    gratuity_display.short_description = "Gratuity"

    def additional_display(self, obj):
        if obj.additional is not None:
            return format_html('<span style="color: purple;">${0}</span>', obj.additional)
        return format_html('<span style="color: gray;">-</span>')
    
    additional_display.short_description = "Additional"

    def amount_display(self, obj):
        if obj.amount >= 0:
            return format_html('<span style="color: green;">${0}</span>', obj.amount)
        else:
            return format_html('<span style="color: red;">${0}</span>', abs(obj.amount))

    amount_display.short_description = "Total"

    def profit_display(self, obj):
        profit = obj.leg.profit_estimate or 0
        if profit >= 0:
            return format_html('<span style="color: green;">${0}</span>', profit)
        else:
            return format_html('<span style="color: red;">${0}</span>', abs(profit))

    profit_display.short_description = "Profit"


@admin.register(DriverPayRate)
class DriverPayRateAdmin(admin.ModelAdmin):
    list_display = ["driver", "route", "vehicle", "direction", "base_pay"]
    list_filter = ["driver__driver_type", "direction"]
    search_fields = [
        "driver__profile__first_name",
        "driver__profile__last_name",
    ]


@admin.register(AffiliateProfile)
class AffiliateProfileAdmin(admin.ModelAdmin):
    """Per-affiliate capability / capacity / route-permit config for the Farm-Out Optimizer.
    Rates live in DriverPayRate; this holds the facts rates can't express."""
    list_display = ["driver", "max_vehicle_tier", "capacity_mode", "daily_cap",
                    "no_pickup_at_port_sanford"]
    list_filter = ["capacity_mode", "no_pickup_at_port_sanford", "max_vehicle_tier"]
    list_editable = ["max_vehicle_tier", "capacity_mode", "daily_cap", "no_pickup_at_port_sanford"]
    search_fields = ["driver__profile__first_name", "driver__profile__last_name"]
    autocomplete_fields = ["driver"]


@admin.register(FleetVehicle)
class FleetVehicleAdmin(admin.ModelAdmin):
    list_display = ["vehicle_number", "vehicle_type", "year", "make", "model", "is_active", "samsara_vehicle_id", "notes"]
    list_editable = ["is_active", "samsara_vehicle_id", "notes"]
    search_fields = ["vehicle_number", "make", "model", "samsara_vehicle_id"]
    list_filter = ["is_active", "vehicle_type", "year", "make"]
    # Live position is written only by the Samsara poller — show, don't edit.
    readonly_fields = [
        "samsara_last_location_label", "samsara_movement_status",
        "samsara_last_latitude", "samsara_last_longitude",
        "samsara_last_seen_at", "samsara_last_synced_at",
    ]


@admin.register(DriverDateOverride)
class DriverDateOverrideAdmin(admin.ModelAdmin):
    list_display = ["driver", "date", "end_date", "exception_type", "status", "start_time", "end_time", "reason", "notes", "created_by"]
    list_filter = ["status", "exception_type", "reason", "submitted_by_driver", "driver"]
    search_fields = [
        "driver__profile__first_name",
        "driver__profile__last_name",
        "notes",
    ]
    date_hierarchy = "date"
    autocomplete_fields = ["driver"]
    readonly_fields = ["created_at", "updated_at", "decided_at"]

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(DriverPaymentExport)
class DriverPaymentExportAdmin(admin.ModelAdmin):
    list_display = [
        "created_at", "created_by", "from_date", "to_date",
        "selected_driver_count", "total_amount", "csv_file_name",
    ]
    list_filter = ["created_by"]
    search_fields = ["csv_file_name", "created_by__username"]
    date_hierarchy = "created_at"
    readonly_fields = [
        "created_at", "created_by", "from_date", "to_date", "csv_file_name",
        "selected_driver_count", "total_amount", "exported_payment_ids",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(DriverPayoutAdjustment)
class DriverPayoutAdjustmentAdmin(admin.ModelAdmin):
    list_display = [
        "created_at", "created_by", "payment", "adjustment_type",
        "old_amount", "new_amount", "delta", "leg",
        "statement_was_emailed", "statement_was_exported",
    ]
    list_filter = ["adjustment_type", "statement_was_emailed", "statement_was_exported"]
    search_fields = [
        "payment__id", "leg__id", "reason", "created_by__username",
    ]
    date_hierarchy = "created_at"
    readonly_fields = [
        "payment", "leg_payment", "leg", "adjustment_type",
        "old_amount", "new_amount", "delta", "reason",
        "created_at", "created_by",
        "statement_was_emailed", "statement_was_exported",
    ]
    autocomplete_fields = []

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
