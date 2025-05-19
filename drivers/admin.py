from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.db.models import Sum, F, Q
from django.utils.safestring import mark_safe
from .models import Driver, DriverPayment, LegPayment
from reservations.models import Leg
from decimal import Decimal


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = [
        "driver_name",
        "email",
        "vehicle",
        "unpaid_legs_count",
        "unpaid_amount",
        "total_paid",
        "total_legs",
        "profit_performance"
    ]
    list_filter = [
        "profile__is_active",
        "payment_method",
    ]
    search_fields = [
        "profile__username",
        "profile__first_name",
        "profile__last_name",
        "profile__email",
        "vehicle",
    ]

    fieldsets = (
        (
            "Driver Information",
            {"fields": ("profile", "vehicle", "schedule", "payment_method")},
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

    def driver_name(self, obj):
        return (
            f"{obj.profile.first_name} {obj.profile.last_name}"
            if obj.profile.first_name
            else obj.profile.username
        )

    driver_name.short_description = "Name"

    def email(self, obj):
        return obj.profile.email

    email.short_description = "Email"

    def unpaid_legs_count(self, obj):
        count = obj.get_unpaid_legs().count()
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
        amount = obj.get_total_unpaid_amount()
        if amount > 0:
            return format_html(
                '<span style="color: green;">${0}</span>', amount
            )
        elif amount < 0:
            return format_html(
                '<span style="color: red;">${0}</span>', abs(amount)
            )
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
        # Sum from DriverPayment records
        payment_amount = obj.payments.aggregate(total=Sum("amount"))["total"] or 0
        
        # Sum from individual legs marked as paid but not in payment records
        paid_legs = Leg.objects.filter(driver=obj, payment_status="paid")
        
        # Exclude legs that are already in payment records to avoid double counting
        leg_ids_in_payments = LegPayment.objects.filter(
            payment__driver=obj
        ).values_list('leg_id', flat=True)
        
        standalone_paid_legs = paid_legs.exclude(id__in=leg_ids_in_payments)
        standalone_amount = sum(leg.driver_pay_amount or 0 for leg in standalone_paid_legs)
        
        # Total amount is the sum of both
        total_amount = payment_amount + standalone_amount
        
        # Color based on amount - using string formatting correctly
        if total_amount > 0:
            return format_html('<span style="color: green;">${0}</span>', total_amount)
        elif total_amount < 0:
            return format_html('<span style="color: red;">${0}</span>', abs(total_amount))
        else:
            return format_html('<span style="color: green;">$0.00</span>')

    total_paid.short_description = "Total Paid"

    def total_legs(self, obj):
        count = obj.legs.count()
        url = (
            reverse("admin:reservations_leg_changelist")
            + f"?driver__id__exact={obj.id}"
        )
        return format_html('<a href="{}">{}</a>', url, count)

    total_legs.short_description = "Total Legs"

    def profit_performance(self, obj):
        """Display driver's profit performance based on their assigned legs"""
        legs = obj.legs.all()
        if not legs:
            return "N/A"
            
        total_profit = sum(leg.profit_estimate or 0 for leg in legs)
        completed_legs = legs.filter(status="completed")
        completed_profit = sum(leg.profit_estimate or 0 for leg in completed_legs)
        
        if total_profit >= 0:
            return format_html('<span style="color: green;">${0}</span>', total_profit)
        else:
            return format_html('<span style="color: red;">${0}</span>', abs(total_profit))
    
    profit_performance.short_description = "Profit"

    def profit_summary(self, obj):
        """Detailed profit information for this driver"""
        legs = obj.legs.all()
        if not legs:
            return "No legs assigned to this driver"
            
        # Calculate various profit metrics
        total_legs = legs.count()
        completed_legs = legs.filter(status="completed").count()
        total_revenue = sum(leg.revenue_share or 0 for leg in legs)
        total_driver_pay = sum(leg.driver_pay_amount or 0 for leg in legs)
        total_profit = sum(leg.profit_estimate or 0 for leg in legs)
        avg_profit_per_leg = total_profit / total_legs if total_legs > 0 else 0
        
        # Calculate profit by status
        status_profits = {}
        for status_choice in [choice[0] for choice in Leg._meta.get_field('status').choices]:
            status_legs = legs.filter(status=status_choice)
            if status_legs.exists():
                status_profit = sum(leg.profit_estimate or 0 for leg in status_legs)
                status_profits[status_choice] = (status_legs.count(), status_profit)
        
        # Create HTML for display
        html = '<h3>Profit Summary</h3>'
        html += '<table style="width: 100%; border-collapse: collapse;">'
        
        # Overall stats
        html += '<tr style="background-color: #f0f0f0;"><th colspan="2">Overall Statistics</th></tr>'
        html += f'<tr><td>Total Legs:</td><td>{total_legs}</td></tr>'
        html += f'<tr><td>Completed Legs:</td><td>{completed_legs}</td></tr>'
        html += f'<tr><td>Total Revenue Share:</td><td>${total_revenue}</td></tr>'
        
        # Format driver pay with color
        if total_driver_pay >= 0:
            driver_pay_html = f'<span style="color: green;">${total_driver_pay}</span>'
        else:
            driver_pay_html = f'<span style="color: red;">${abs(total_driver_pay)}</span>'
        html += f'<tr><td>Total Driver Pay:</td><td>{driver_pay_html}</td></tr>'
        
        # Format total profit with color
        if total_profit >= 0:
            profit_html = f'<span style="color: green;">${total_profit}</span>'
        else:
            profit_html = f'<span style="color: red;">${abs(total_profit)}</span>'
        html += f'<tr><td>Total Profit:</td><td>{profit_html}</td></tr>'
        
        # Format avg profit with color
        if avg_profit_per_leg >= 0:
            avg_profit_html = f'<span style="color: green;">${avg_profit_per_leg}</span>'
        else:
            avg_profit_html = f'<span style="color: red;">${abs(avg_profit_per_leg)}</span>'
        html += f'<tr><td>Average Profit per Leg:</td><td>{avg_profit_html}</td></tr>'
        
        # Profit by status
        if status_profits:
            html += '<tr style="background-color: #f0f0f0;"><th colspan="2">Profit by Status</th></tr>'
            for status, (count, profit) in status_profits.items():
                status_display = status.title() if status else "Unknown"
                if profit >= 0:
                    profit_html = f'<span style="color: green;">${profit}</span>'
                else:
                    profit_html = f'<span style="color: red;">${abs(profit)}</span>'
                html += f'<tr><td>{status_display} ({count}):</td><td>{profit_html}</td></tr>'
        
        html += '</table>'
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
            
            # Get driver pay amount
            amount = leg.driver_pay_amount or 0
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

    def preview_driver_payments(self, request, queryset):
        """Show a preview of driver payments without actually processing them."""
        from django.contrib import messages

        preview_data = []
        total_amount = 0

        for driver in queryset:
            # Get unpaid completed legs
            unpaid_legs = driver.get_unpaid_legs()

            if unpaid_legs:
                # Calculate total
                payment_total = sum(leg.driver_pay_amount or 0 for leg in unpaid_legs)

                # Count legs
                leg_count = unpaid_legs.count()

                # Calculate total profit for these legs
                profit_total = sum(leg.profit_estimate or 0 for leg in unpaid_legs)

                # Get date range
                leg_dates = [leg.pickup_date for leg in unpaid_legs]
                if leg_dates:  # Check if there are actually dates to get min/max from
                    start_date = min(leg_dates)
                    end_date = max(leg_dates)

                    # Add to preview data
                    preview_data.append(
                        {
                            "driver": str(driver),
                            "amount": payment_total,
                            "profit": profit_total,
                            "count": leg_count,
                            "period": f"{start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}",
                        }
                    )

                    total_amount += payment_total

        if preview_data:
            # Create a message with the preview data
            message = "Driver Payment Preview:<br><br>"
            message += "<table style='border-collapse: collapse; width: 100%;'>"
            message += "<tr style='background-color: #f2f2f2;'><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Driver</th><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Legs</th><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Period</th><th style='padding: 8px; text-align: right; border: 1px solid #ddd;'>Amount</th><th style='padding: 8px; text-align: right; border: 1px solid #ddd;'>Profit</th></tr>"

            for item in preview_data:
                # Format amounts with color
                amount_color = "green" if item["amount"] >= 0 else "red"
                profit_color = "green" if item["profit"] >= 0 else "red"
                
                message += f"""<tr>
                    <td style='padding: 8px; border: 1px solid #ddd;'>{item['driver']}</td>
                    <td style='padding: 8px; border: 1px solid #ddd;'>{item['count']}</td>
                    <td style='padding: 8px; border: 1px solid #ddd;'>{item['period']}</td>
                    <td style='padding: 8px; text-align: right; border: 1px solid #ddd;'>
                        <span style='color: {amount_color};'>${item['amount']}</span>
                    </td>
                    <td style='padding: 8px; text-align: right; border: 1px solid #ddd;'>
                        <span style='color: {profit_color};'>${item['profit']}</span>
                    </td>
                </tr>"""

            # Calculate total profit
            total_profit = sum(item["profit"] for item in preview_data)
            
            # Format totals with color
            total_amount_color = "green" if total_amount >= 0 else "red"
            total_profit_color = "green" if total_profit >= 0 else "red"
            
            message += f"""<tr style='background-color: #f2f2f2;'>
                <td colspan='3' style='padding: 8px; border: 1px solid #ddd; text-align: right;'>
                    <strong>Total:</strong>
                </td>
                <td style='padding: 8px; border: 1px solid #ddd; text-align: right;'>
                    <strong><span style='color: {total_amount_color};'>${total_amount}</span></strong>
                </td>
                <td style='padding: 8px; border: 1px solid #ddd; text-align: right;'>
                    <strong><span style='color: {total_profit_color};'>${total_profit}</span></strong>
                </td>
            </tr>"""
            
            message += "</table><br>"
            message += "To process these payments, select the drivers again and use the 'Process driver payments' action."

            self.message_user(request, mark_safe(message))
        else:
            self.message_user(request, "No unpaid legs found for the selected drivers.")

    preview_driver_payments.short_description = "Preview driver payments"

    def process_driver_payments(self, request, queryset):
        """Process payments for all unpaid legs for selected drivers."""
        from django.contrib import messages
        from django.utils import timezone
        import logging

        logger = logging.getLogger(__name__)
        processed_count = 0
        total_amount = 0

        for driver in queryset:
            # Get unpaid completed legs
            unpaid_legs = driver.get_unpaid_legs()
            
            # Log the number of unpaid legs found
            logger.info(f"Driver {driver} has {unpaid_legs.count()} unpaid legs")

            if unpaid_legs:
                # Group legs by reservation
                reservation_legs = {}
                for leg in unpaid_legs:
                    if leg.reservation:
                        if leg.reservation not in reservation_legs:
                            reservation_legs[leg.reservation] = []
                        reservation_legs[leg.reservation].append(leg)

                # Calculate total
                payment_total = sum(leg.driver_pay_amount or 0 for leg in unpaid_legs)

                # Create detailed notes
                notes = []
                notes.append(f"Automatic payment for {unpaid_legs.count()} legs")
                notes.append("\nReservation details:")
                
                for reservation, legs in reservation_legs.items():
                    leg_total = sum(leg.driver_pay_amount or 0 for leg in legs)
                    notes.append(
                        f"\n- Reservation #{reservation.id}: {reservation.customer.get_full_name()}"
                        f" ({len(legs)} legs, ${leg_total})"
                    )
                    
                    # Add detailed leg information
                    for leg in legs:
                        notes.append(
                            f"  • {leg.pickup_date.strftime('%Y-%m-%d %H:%M')}"
                            f" | {leg.pickup_location} → {leg.dropoff_location}"
                            f" | Pay: ${leg.driver_pay_amount or 0}"
                            f" | Profit: ${leg.profit_estimate or 0}"
                            f" | Status: {leg.status}"
                        )

                try:
                    # Create payment record using the class method
                    logger.info(f"Creating payment for {driver} with {unpaid_legs.count()} legs")
                    
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
                    logger.info(f"Payment created with ID {payment.id}, with {leg_payment_count} leg payments")
                    
                    processed_count += 1
                    total_amount += payment_total

                except Exception as e:
                    logger.error(f"Error processing payment for {driver}: {e}", exc_info=True)
                    messages.error(request, f"Error processing {driver}: {e}")

        if processed_count:
            messages.success(
                request,
                f"Processed payments for {processed_count} drivers. Total: ${total_amount}",
            )
        else:
            messages.info(request, "No unpaid legs found for selected drivers.")

    process_driver_payments.short_description = "Process driver payments"


@admin.register(DriverPayment)
class DriverPaymentAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "driver_link",
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

    def driver_link(self, obj):
        url = reverse("admin:drivers_driver_change", args=[obj.driver.id])
        return format_html('<a href="{}">{}</a>', url, obj.driver)

    driver_link.short_description = "Driver"

    def amount_display(self, obj):
        # Format with color based on value
        if obj.amount >= 0:
            return format_html('<span style="color: green;">${0}</span>', obj.amount)
        else:
            return format_html('<span style="color: red;">${0}</span>', abs(obj.amount))

    amount_display.short_description = "Amount"

    def leg_count(self, obj):
        count = obj.leg_payments.count()
        return count

    leg_count.short_description = "Legs Paid"

    readonly_fields = ["payment_date", "leg_details"]

    fieldsets = (
        (
            "Payment Information",
            {
                "fields": (
                    "driver",
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
    list_display = ["id", "payment", "leg_display", "amount_display", "profit_display"]
    search_fields = [
        "payment__driver__profile__first_name",
        "payment__driver__profile__last_name",
        "leg__pickup_location",
        "leg__dropoff_location",
    ]
    list_filter = ["payment__payment_method", "payment__payment_date"]

    def leg_display(self, obj):
        url = reverse("admin:reservations_leg_change", args=[obj.leg.id])
        return format_html('<a href="{}">{} - {}</a>', url, obj.leg.id, obj.leg.pickup_date)

    leg_display.short_description = "Leg"

    def amount_display(self, obj):
        if obj.amount >= 0:
            return format_html('<span style="color: green;">${0}</span>', obj.amount)
        else:
            return format_html('<span style="color: red;">${0}</span>', abs(obj.amount))

    amount_display.short_description = "Amount"
    
    def profit_display(self, obj):
        profit = obj.leg.profit_estimate or 0
        if profit >= 0:
            return format_html('<span style="color: green;">${0}</span>', profit)
        else:
            return format_html('<span style="color: red;">${0}</span>', abs(profit))
            
    profit_display.short_description = "Profit"