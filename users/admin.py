"""
Django Admin Configuration for User and Agency Management

This module provides comprehensive admin interfaces for managing travel agents,
agencies, commission payouts, and related models with advanced features for
commission processing and financial tracking.
"""

from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.db.models import Sum, Count, Q, Exists, OuterRef
from django.utils.safestring import mark_safe
from django.utils import timezone
from decimal import Decimal
from django.contrib import messages
import logging

from .models import (
    UserProfile,
    PartnerForm,
    ContactUsForm,
    NewsLetter,
    TravelAgent,
    CommissionPayout,
    Agency,
    AgencyCommissionPayout,
)
from dispatching.admin_mixins import DispatcherAdminMixin

logger = logging.getLogger(__name__)

# =============================================
# SIMPLE MODEL REGISTRATIONS
# =============================================

admin.site.register([UserProfile, PartnerForm, NewsLetter])


# =============================================
# UTILITY FUNCTIONS
# =============================================


def format_currency(amount, highlight_positive=False, positive_color="red"):
    """Format currency with optional highlighting for positive amounts."""
    try:
        amount = float(amount or 0)
        amount_str = f"${amount:,.2f}"

        if highlight_positive and amount > 0:
            return format_html(
                f'<span style="color: {positive_color}; font-weight: bold;">{amount_str}</span>'
            )
        elif amount == 0:
            return format_html(f'<span style="color: green;">{amount_str}</span>')
        else:
            return format_html(f'<span style="color: gray;">{amount_str}</span>')
    except (ValueError, TypeError):
        return str(amount)


def create_admin_link(model_name, obj_id, display_text, app_label="users"):
    """Create a formatted admin link."""
    url = reverse(f"admin:{app_label}_{model_name}_change", args=[obj_id])
    return format_html('<a href="{}">{}</a>', url, display_text)


def create_changelist_link(model_name, filter_params, display_text, app_label="users"):
    """Create a link to admin changelist with filters."""
    url = reverse(f"admin:{app_label}_{model_name}_changelist") + f"?{filter_params}"
    return format_html('<a href="{}">{}</a>', url, display_text)


# =============================================
# INLINE ADMIN CLASSES
# =============================================


class ReservationInline(admin.TabularInline):
    """Inline for displaying reservations in commission payouts."""

    model = CommissionPayout.reservations.through
    extra = 0
    readonly_fields = ["reservation_info"]
    verbose_name = "Reservation"
    verbose_name_plural = "Reservations in this Payout"

    def get_queryset(self, request):
        """Optimize queryset with related data."""
        from reservations.models import Reservation
        qs = super().get_queryset(request)
        return qs.select_related("reservation__customer")

    def reservation_info(self, obj):
        """Display formatted reservation information."""
        reservation = obj.reservation
        customer_name = str(reservation.customer) if reservation.customer else "N/A"
        return create_admin_link(
            "reservation",
            reservation.id,
            f"#{reservation.id} - {customer_name} - ${reservation.total_price:.2f}",
            app_label="reservations",
        )

    reservation_info.short_description = "Reservation Details"


class AgentPayoutInline(admin.TabularInline):
    """Inline for displaying agent payouts in agency payouts."""

    model = AgencyCommissionPayout.agent_payouts.through
    extra = 0
    readonly_fields = ["agent_payout_info"]
    verbose_name = "Agent Payout"
    verbose_name_plural = "Agent Payouts in this Agency Payout"

    def agent_payout_info(self, obj):
        """Display formatted agent payout information."""
        payout = obj.commissionpayout
        return create_admin_link(
            "commissionpayout",
            payout.id,
            f"#{payout.id} - {payout.agent} - ${payout.total_amount:.2f}",
        )

    agent_payout_info.short_description = "Agent Payout Details"


# =============================================
# CUSTOM FILTERS
# =============================================

class HasUnpaidReservationsFilter(admin.SimpleListFilter):
    """Filter to show agents with unpaid reservations."""
    
    title = 'Unpaid Reservations'
    parameter_name = 'has_unpaid_reservations'
    
    def lookups(self, request, model_admin):
        return (
            ('yes', 'Has Unpaid Reservations'),
            ('no', 'No Unpaid Reservations'),
        )
    
    def queryset(self, request, queryset):
        from reservations.models import Reservation
        
        if self.value() == 'yes':
            # Filter for agents with unpaid completed reservations
            return queryset.filter(
                reservations__commission_paid=False,
                reservations__status='completed'
            ).distinct()
        elif self.value() == 'no':
            # Filter for agents without unpaid completed reservations
            return queryset.exclude(
                reservations__commission_paid=False,
                reservations__status='completed'
            ).distinct()
        return queryset


class UnpaidCommissionAmountFilter(admin.SimpleListFilter):
    """Filter agents by unpaid commission amount ranges."""
    
    title = 'Unpaid Commission Amount'
    parameter_name = 'unpaid_amount'
    
    def lookups(self, request, model_admin):
        return (
            ('0', 'No Unpaid Commission'),
            ('1-50', '$1 - $50'),
            ('50-100', '$50 - $100'),
            ('100-500', '$100 - $500'),
            ('500+', '$500+'),
        )
    
    def queryset(self, request, queryset):
        if self.value() == '0':
            return queryset.filter(unpaid_commissions=0)
        elif self.value() == '1-50':
            return queryset.filter(unpaid_commissions__gt=0, unpaid_commissions__lte=50)
        elif self.value() == '50-100':
            return queryset.filter(unpaid_commissions__gt=50, unpaid_commissions__lte=100)
        elif self.value() == '100-500':
            return queryset.filter(unpaid_commissions__gt=100, unpaid_commissions__lte=500)
        elif self.value() == '500+':
            return queryset.filter(unpaid_commissions__gt=500)
        return queryset


# =============================================
# TRAVEL AGENT ADMIN
# =============================================


@admin.register(TravelAgent)
class TravelAgentAdmin(admin.ModelAdmin):
    """Admin interface for travel agents with commission management."""

    list_display = [
        "user",
        "agent_name",
        "agency_name",
        "agency",
        "agency_handles_payment",
        "commission_rate",
        "unpaid_commission",
        "pending_commission",
        "total_paid",
        "total_reservations",
        "is_active",
    ]
    list_filter = ["is_active", "agency_handles_payment", "agency", "created_at", HasUnpaidReservationsFilter, UnpaidCommissionAmountFilter]
    search_fields = [
        "user__username",
        "agent_name",
        "agency_name",
        "user__email",
        "agency__name",
    ]
    list_editable = ["agency", "agency_handles_payment", "commission_rate"]
    list_per_page = 50

    fieldsets = (
        (
            "Agent Information",
            {
                "fields": (
                    "user",
                    "agent_name",
                    "agency",
                    "agency_name",
                    "phone",
                    "is_active",
                )
            },
        ),
        (
            "Payment Configuration",
            {
                "fields": (
                    "payment_method",
                    "payment_info",
                    "commission_rate",
                    "agency_handles_payment",
                )
            },
        ),
        (
            "Commission Tracking",
            {
                "fields": (
                    "total_paid_commission",
                    "pending_commissions",
                    "unpaid_commissions",
                    "last_payment_date",
                ),
                "description": "Commission values are automatically calculated based on reservations and payment history.",
                "classes": ("collapse",),
            },
        ),
        ("System Information", {"fields": ("created_at",), "classes": ("collapse",)}),
    )
    readonly_fields = ["created_at"]

    def get_queryset(self, request):
        """Optimize queryset with related data."""
        return super().get_queryset(request).select_related("user", "agency")

    def total_reservations(self, obj):
        """Display total reservations with link to filtered list."""
        from reservations.models import Reservation

        count = Reservation.objects.filter(travel_agent=obj).count()
        return create_changelist_link(
            "reservation",
            f"travel_agent__id__exact={obj.id}",
            str(count),
            app_label="reservations",
        )

    total_reservations.short_description = "Reservations"

    def total_paid(self, obj):
        """Display formatted total paid commission."""
        return format_currency(obj.total_paid_commission)

    total_paid.short_description = "Paid"

    def unpaid_commission(self, obj):
        """Display unpaid commission with red highlighting for positive amounts."""
        return format_currency(obj.unpaid_commissions, highlight_positive=True)

    unpaid_commission.short_description = "Unpaid"

    def pending_commission(self, obj):
        """Display pending commission with blue highlighting for positive amounts."""
        return format_currency(
            obj.pending_commissions, highlight_positive=True, positive_color="blue"
        )

    pending_commission.short_description = "Pending"

    # Admin Actions
    actions = [
        "preview_commission_payments",
        "process_agent_commissions",
        "process_dual_commissions",
        "update_commission_stats",
        "mark_agents_for_payment",
    ]

    def preview_commission_payments(self, request, queryset):
        """Preview commission payments without processing them."""
        preview_data = self._calculate_commission_preview(queryset)

        if preview_data["agents"]:
            message = self._format_preview_message(preview_data)
            self.message_user(request, mark_safe(message))
        else:
            self.message_user(
                request, "No unpaid commissions found for selected agents."
            )

    preview_commission_payments.short_description = "Preview commission payments"

    def process_agent_commissions(self, request, queryset):
        """Process commissions for agents (agent payouts only)."""
        self._process_commissions(request, queryset, create_agency_payout=False)

    process_agent_commissions.short_description = "Process agent commissions only"

    def process_dual_commissions(self, request, queryset):
        """Process commissions with dual payouts (agent + agency where applicable)."""
        self._process_commissions(request, queryset, create_agency_payout=True)

    process_dual_commissions.short_description = "Process dual payouts (Agent + Agency)"

    def update_commission_stats(self, request, queryset):
        """Update commission statistics for selected agents."""
        updated_count = 0
        
        for agent in queryset:
            try:
                stats = agent.update_commission_stats()
                updated_count += 1
                logger.info(f"Updated commission stats for {agent}: {stats}")
            except Exception as e:
                logger.error(f"Error updating stats for {agent}: {e}")
                messages.error(request, f"Error updating {agent}: {e}")
        
        if updated_count:
            messages.success(request, f"Updated commission statistics for {updated_count} agents.")
        else:
            messages.info(request, "No agents selected or no updates needed.")

    update_commission_stats.short_description = "Update commission statistics"

    def mark_agents_for_payment(self, request, queryset):
        """Mark agents with unpaid commissions for payment processing."""
        agents_with_unpaid = queryset.filter(unpaid_commissions__gt=0)
        
        if agents_with_unpaid.exists():
            total_unpaid = sum(agent.unpaid_commissions for agent in agents_with_unpaid)
            message = f"Found {agents_with_unpaid.count()} agents with unpaid commissions totaling ${total_unpaid:,.2f}. "
            message += "Use 'Preview commission payments' to see details, then 'Process agent commissions' to pay them."
            messages.info(request, message)
        else:
            messages.info(request, "No agents with unpaid commissions found in selection.")

    mark_agents_for_payment.short_description = "Check agents for payment"

    def _calculate_commission_preview(self, queryset):
        """Calculate commission preview data."""
        from django.db.models import Sum, F, ExpressionWrapper, DecimalField
        from reservations.models import Reservation

        preview_data = {"agents": [], "total": 0}

        for agent in queryset:
            unpaid_reservations = Reservation.objects.filter(
                travel_agent=agent, commission_paid=False, status="completed"
            ).annotate(
                calculated_commission=ExpressionWrapper(
                    F("total_price") * (agent.commission_rate / 100),
                    output_field=DecimalField(max_digits=10, decimal_places=2),
                )
            )

            if unpaid_reservations.exists():
                commission_total = sum(
                    r.calculated_commission for r in unpaid_reservations
                )
                # Get date range based on actual service dates (pickup dates) and current date
                earliest_pickup_date = None
                for reservation in unpaid_reservations:
                    for leg in reservation.legs.all():
                        if earliest_pickup_date is None or leg.pickup_date < earliest_pickup_date:
                            earliest_pickup_date = leg.pickup_date
                
                start_date = earliest_pickup_date if earliest_pickup_date else timezone.localtime(timezone.now()).date()
                end_date = timezone.localtime(timezone.now()).date()  # Current date when processing payout

                preview_data["agents"].append(
                    {
                        "agent": agent,
                        "amount": commission_total,
                        "count": unpaid_reservations.count(),
                        "period": f"{start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}",
                        "agency_handles": agent.agency_handles_payment and agent.agency,
                    }
                )
                preview_data["total"] += commission_total

        return preview_data

    def _format_preview_message(self, preview_data):
        """Format preview message as HTML table."""
        message = "Commission Payment Preview:<br><br>"
        message += "<table style='border-collapse: collapse; width: 100%;'>"
        message += "<tr style='background-color: #f2f2f2;'>"
        message += "<th style='padding: 8px; border: 1px solid #ddd;'>Agent</th>"
        message += "<th style='padding: 8px; border: 1px solid #ddd;'>Reservations</th>"
        message += "<th style='padding: 8px; border: 1px solid #ddd;'>Period</th>"
        message += "<th style='padding: 8px; border: 1px solid #ddd;'>Amount</th>"
        message += (
            "<th style='padding: 8px; border: 1px solid #ddd;'>Payment To</th></tr>"
        )

        for item in preview_data["agents"]:
            payment_to = "Agency" if item["agency_handles"] else "Agent"
            message += (
                f"<tr>"
                f"<td style='padding: 8px; border: 1px solid #ddd;'>{item['agent']}</td>"
                f"<td style='padding: 8px; border: 1px solid #ddd;'>{item['count']}</td>"
                f"<td style='padding: 8px; border: 1px solid #ddd;'>{item['period']}</td>"
                f"<td style='padding: 8px; border: 1px solid #ddd;'>${item['amount']:,.2f}</td>"
                f"<td style='padding: 8px; border: 1px solid #ddd;'>{payment_to}</td>"
                f"</tr>"
            )

        message += (
            f"<tr style='background-color: #f2f2f2;'>"
            f"<td colspan='3' style='padding: 8px; border: 1px solid #ddd;'><strong>Total:</strong></td>"
            f"<td style='padding: 8px; border: 1px solid #ddd;'><strong>${preview_data['total']:,.2f}</strong></td>"
            f"<td style='padding: 8px; border: 1px solid #ddd;'></td></tr>"
            f"</table><br>"
            "Use 'Process agent commissions' or 'Process dual payouts' to execute payments."
        )
        return message

    def _process_commissions(self, request, queryset, create_agency_payout):
        """Process commissions for selected agents."""
        processed_count = 0
        total_amount = 0
        agency_payouts_created = 0
        warnings = []

        for agent in queryset:
            try:
                # Validate agency payment settings
                if create_agency_payout:
                    if not agent.agency:
                        warnings.append(
                            f"{agent} has no agency. Processing agent payout only."
                        )
                        create_agency = False
                    elif not agent.agency_handles_payment:
                        warnings.append(
                            f"{agent} not set for agency payment. Processing agent payout only."
                        )
                        create_agency = False
                    else:
                        create_agency = True
                else:
                    create_agency = False

                payout, amount, agency_payout = agent.process_commission_payment(
                    create_agency_payout=create_agency
                )

                if payout:
                    processed_count += 1
                    total_amount += amount
                    if agency_payout:
                        agency_payouts_created += 1
                    logger.info(f"Processed commission for {agent}: ${amount}")

            except Exception as e:
                logger.error(f"Error processing {agent}: {e}")
                messages.error(request, f"Error processing {agent}: {e}")

        # Display results
        for warning in warnings:
            messages.warning(request, warning)

        if processed_count:
            message = (
                f"Processed {processed_count} agents. Total: ${total_amount:,.2f}."
            )
            if agency_payouts_created:
                message += f" Created {agency_payouts_created} agency payouts."
            messages.success(request, message)
        else:
            messages.info(request, "No unpaid commissions found.")


# =============================================
# COMMISSION PAYOUT ADMIN
# =============================================


@admin.register(CommissionPayout)
class CommissionPayoutAdmin(DispatcherAdminMixin, admin.ModelAdmin):
    """Admin interface for individual agent commission payouts - hidden from dispatchers."""
    
    def has_module_permission(self, request):
        """Only show to superusers."""
        return request.user.is_superuser
    
    def has_view_permission(self, request, obj=None):
        """Only superusers can view."""
        return request.user.is_superuser
    
    def has_add_permission(self, request):
        """Only superusers can add."""
        return request.user.is_superuser
    
    def has_change_permission(self, request, obj=None):
        """Only superusers can change."""
        return request.user.is_superuser
    
    def has_delete_permission(self, request, obj=None):
        """Only superusers can delete."""
        return request.user.is_superuser

    list_display = [
        "payout_id",
        "agent_link",
        "agency_link",
        "total_amount",
        "reservation_count",
        "payout_period",
        "paid_at",
        "payment_status",
    ]
    list_filter = ["paid_at", "payout_period_start", "agency"]
    search_fields = ["agent__agent_name", "agent__user__username", "agency__name"]
    inlines = [ReservationInline]
    readonly_fields = ["paid_at", "reservation_details"]
    list_per_page = 50

    @admin.action(description="Send commission statement email")
    def send_commission_statement(self, request, queryset):
        from users.emails import send_agent_commission_statement

        sent = 0
        failed = 0
        for payout in queryset.select_related("agent__user"):
            agent = payout.agent
            email = agent.user.email if agent.user else None
            if not email:
                failed += 1
                continue
            if send_agent_commission_statement(agent=agent, payout=payout, recipient_email=email):
                sent += 1
            else:
                failed += 1

        if sent:
            self.message_user(request, f"Sent {sent} statement(s) successfully.")
        if failed:
            self.message_user(request, f"Failed to send {failed} statement(s).", level="error")

    fieldsets = (
        (
            "Payout Information",
            {
                "fields": (
                    "agent",
                    "agency",
                    "total_amount",
                    "payout_period_start",
                    "payout_period_end",
                )
            },
        ),
        ("Payment Details", {"fields": ("paid_at", "notes")}),
        (
            "Reservation Details",
            {"fields": ("reservation_details",), "classes": ("collapse",)},
        ),
    )

    def get_queryset(self, request):
        """Optimize queryset with related data."""
        from django.db.models import Prefetch
        from reservations.models import Reservation
        return (
            super()
            .get_queryset(request)
            .select_related("agent__user", "agency")
            .prefetch_related(
                Prefetch(
                    "reservations",
                    queryset=Reservation.objects.select_related("customer").only(
                        "id", "customer__first_name", "customer__last_name", 
                        "customer__email", "total_price", "commission_amount", 
                        "created_at"
                    )
                )
            )
        )

    def payout_id(self, obj):
        """Display payout ID with # prefix."""
        return f"#{obj.id}"

    payout_id.short_description = "ID"

    def agent_link(self, obj):
        """Display agent name as admin link."""
        return create_admin_link("travelagent", obj.agent.id, str(obj.agent))

    agent_link.short_description = "Agent"

    def agency_link(self, obj):
        """Display agency name as admin link or dash if none."""
        if obj.agency:
            return create_admin_link("agency", obj.agency.id, obj.agency.name)
        return "-"

    agency_link.short_description = "Agency"

    def payout_period(self, obj):
        """Display formatted payout period."""
        return format_html(
            "{} to {}",
            obj.payout_period_start.strftime("%b %d, %Y"),
            obj.payout_period_end.strftime("%b %d, %Y"),
        )

    payout_period.short_description = "Period"

    def reservation_count(self, obj):
        """Display count of reservations in payout."""
        return obj.reservations.count()

    reservation_count.short_description = "Trips"

    def payment_status(self, obj):
        """Display payment status with color coding."""
        return format_html('<span style="color: green;">✓ Paid</span>')

    payment_status.short_description = "Status"

    def reservation_details(self, obj):
        """Display detailed reservation table."""
        # Use prefetched reservations from get_queryset
        reservations = obj.reservations.all()
        if not reservations:
            return "No reservations"

        html = "<table style='width: 100%; border-collapse: collapse;'>"
        html += "<tr style='background-color: #f0f0f0;'>"
        html += "<th style='padding: 4px; border: 1px solid #ddd;'>ID</th>"
        html += "<th style='padding: 4px; border: 1px solid #ddd;'>Customer</th>"
        html += "<th style='padding: 4px; border: 1px solid #ddd;'>Date</th>"
        html += "<th style='padding: 4px; border: 1px solid #ddd;'>Amount</th>"
        html += "<th style='padding: 4px; border: 1px solid #ddd;'>Commission</th>"
        html += "</tr>"

        for res in reservations:
            edit_link = create_admin_link(
                "reservation", res.id, f"#{res.id}", app_label="reservations"
            )
            customer_name = str(res.customer) if res.customer else "N/A"
            created_date = res.created_at.strftime('%b %d, %Y') if res.created_at else "N/A"
            html += (
                f"<tr>"
                f"<td style='padding: 4px; border: 1px solid #ddd;'>{edit_link}</td>"
                f"<td style='padding: 4px; border: 1px solid #ddd;'>{customer_name}</td>"
                f"<td style='padding: 4px; border: 1px solid #ddd;'>{created_date}</td>"
                f"<td style='padding: 4px; border: 1px solid #ddd;'>${res.total_price:.2f}</td>"
                f"<td style='padding: 4px; border: 1px solid #ddd;'>${res.commission_amount:.2f}</td>"
                f"</tr>"
            )

        html += "</table>"
        return mark_safe(html)

    reservation_details.short_description = "Reservation Details"

    actions = ["recalculate_amounts", "cancel_payouts", "send_commission_statement"]

    def recalculate_amounts(self, request, queryset):
        """Recalculate payout amounts based on included reservations."""
        updated_count = 0

        for payout in queryset:
            reservations = payout.reservations.all()
            new_total = reservations.aggregate(total=Sum("commission_amount"))[
                "total"
            ] or Decimal("0")

            if new_total != payout.total_amount:
                old_amount = payout.total_amount
                payout.total_amount = new_total
                payout.save()
                updated_count += 1
                messages.success(
                    request,
                    f"Updated payout #{payout.id} from ${old_amount:.2f} to ${new_total:.2f}",
                )

        if not updated_count:
            messages.info(request, "All selected payouts have correct amounts.")

    recalculate_amounts.short_description = "Recalculate payout amounts"

    def cancel_payouts(self, request, queryset):
        """Cancel payouts and return commissions to unpaid status."""
        cancelled_count = 0
        total_cancelled = 0

        for payout in queryset:
            total_cancelled += payout.total_amount
            logger.info(f"Cancelling payout #{payout.id} for ${payout.total_amount}")
            payout.delete()  # Pre-delete signal handles cleanup
            cancelled_count += 1

        if cancelled_count:
            messages.success(
                request,
                f"Cancelled {cancelled_count} payouts totaling ${total_cancelled:.2f}. "
                "Reservations marked as unpaid.",
            )

    cancel_payouts.short_description = "Cancel selected payouts"

    def send_commission_statement(self, request, queryset):
        """Send commission statement emails for selected payouts."""
        from users.emails import send_agent_commission_statement

        sent_count = 0
        failed_count = 0

        for payout in queryset:
            agent = payout.agent
            recipient_email = agent.user.email

            if recipient_email:
                success = send_agent_commission_statement(
                    agent=agent,
                    payout=payout,
                    recipient_email=recipient_email,
                )
                if success:
                    sent_count += 1
                else:
                    failed_count += 1
            else:
                failed_count += 1
                messages.warning(
                    request,
                    f"No email address for agent {agent.agent_name}",
                )

        if sent_count:
            messages.success(
                request,
                f"Successfully sent {sent_count} commission statement(s).",
            )
        if failed_count:
            messages.error(
                request,
                f"Failed to send {failed_count} statement(s). Check email configuration.",
            )

    send_commission_statement.short_description = "Send commission statement email"


# =============================================
# AGENCY ADMIN
# =============================================


@admin.register(Agency)
class AgencyAdmin(admin.ModelAdmin):
    """Admin interface for agencies with comprehensive management features."""

    list_display = [
        "name",
        "agent_count",
        "agents_with_agency_payment",
        "total_unpaid",
        "total_pending",
        "total_paid",
        "is_active",
    ]
    list_filter = ["is_active", "created_at"]
    search_fields = ["name"]
    filter_horizontal = ["heads"]
    list_per_page = 25

    fieldsets = (
        (
            "Agency Information",
            {"fields": ("name", "address", "phone", "website", "logo", "is_active")},
        ),
        (
            "Management",
            {
                "fields": ("heads",),
                "description": "Users who can manage this agency and its agents.",
            },
        ),
        (
            "Payment Information",
            {"fields": ("payment_method", "payment_info", "total_paid_commission")},
        ),
        (
            "System Information",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )
    readonly_fields = ["created_at", "updated_at", "total_paid_commission"]

    def get_queryset(self, request):
        """Optimize queryset with related data."""
        return super().get_queryset(request).prefetch_related("agents", "heads")

    def agent_count(self, obj):
        """Display total agent count with link to filtered list."""
        count = obj.agents.count()
        return create_changelist_link(
            "travelagent", f"agency__id__exact={obj.id}", str(count)
        )

    agent_count.short_description = "Agents"

    def agents_with_agency_payment(self, obj):
        """Display count of agents with agency payment handling."""
        count = obj.agents.filter(agency_handles_payment=True).count()
        if count > 0:
            return create_changelist_link(
                "travelagent",
                f"agency__id__exact={obj.id}&agency_handles_payment__exact=1",
                str(count),
            )
        return "0"

    agents_with_agency_payment.short_description = "Agency Payment"

    def total_unpaid(self, obj):
        """Display total unpaid commissions."""
        return format_currency(
            obj.get_total_unpaid_commissions(), highlight_positive=True
        )

    total_unpaid.short_description = "Unpaid"

    def total_pending(self, obj):
        """Display total pending commissions."""
        return format_currency(
            obj.get_total_pending_commissions(),
            highlight_positive=True,
            positive_color="blue",
        )

    total_pending.short_description = "Pending"

    def total_paid(self, obj):
        """Display total paid commissions."""
        obj.sync_paid_commission()
        return format_currency(obj.total_paid_commission)

    total_paid.short_description = "Paid"

    actions = [
        "process_agency_commissions",
        "preview_agency_commissions",
        "update_commission_stats",
    ]

    def preview_agency_commissions(self, request, queryset):
        """Preview agency commission payments."""
        preview_data = []
        grand_total = 0

        for agency in queryset:
            agents_with_unpaid = agency.agents.filter(
                unpaid_commissions__gt=0, agency_handles_payment=True
            )

            if agents_with_unpaid.exists():
                agency_total = sum(
                    agent.unpaid_commissions for agent in agents_with_unpaid
                )

                preview_data.append(
                    {
                        "agency": agency,
                        "agent_count": agents_with_unpaid.count(),
                        "total": agency_total,
                        "agents": [
                            {"name": str(agent), "unpaid": agent.unpaid_commissions}
                            for agent in agents_with_unpaid
                        ],
                    }
                )
                grand_total += agency_total

        if preview_data:
            message = "Agency Commission Payment Preview:<br><br>"

            for item in preview_data:
                message += f"<h4>{item['agency']} - {item['agent_count']} agents - ${item['total']:,.2f}</h4>"
                message += (
                    "<table style='border-collapse: collapse; margin-bottom: 20px;'>"
                )
                message += (
                    "<tr><th style='padding: 8px; border: 1px solid #ddd;'>Agent</th>"
                )
                message += (
                    "<th style='padding: 8px; border: 1px solid #ddd;'>Unpaid</th></tr>"
                )

                for agent in item["agents"]:
                    message += (
                        f"<tr>"
                        f"<td style='padding: 8px; border: 1px solid #ddd;'>{agent['name']}</td>"
                        f"<td style='padding: 8px; border: 1px solid #ddd;'>${agent['unpaid']:,.2f}</td>"
                        f"</tr>"
                    )
                message += "</table>"

            message += f"<p><strong>Grand Total: ${grand_total:,.2f}</strong></p>"
            self.message_user(request, mark_safe(message))
        else:
            self.message_user(
                request, "No unpaid commissions found for agency payment handling."
            )

    preview_agency_commissions.short_description = "Preview agency payments"

    def process_agency_commissions(self, request, queryset):
        """Process commission payments for agencies."""
        processed_count = 0
        total_amount = 0
        agents_processed = 0

        for agency in queryset:
            try:
                payout, amount = agency.process_agency_commission_payment()
                if payout:
                    processed_count += 1
                    total_amount += amount
                    agents_processed += payout.agent_payouts.count()
                    logger.info(f"Created agency payout #{payout.id} for ${amount}")
            except Exception as e:
                logger.error(f"Error processing {agency}: {e}")
                messages.error(request, f"Error processing {agency}: {e}")

        if processed_count:
            messages.success(
                request,
                f"Processed {processed_count} agencies, {agents_processed} agents. "
                f"Total: ${total_amount:,.2f}",
            )
        else:
            messages.info(request, "No unpaid commissions found.")

    process_agency_commissions.short_description = "Process agency payments"

    def update_commission_stats(self, request, queryset):
        """Update commission statistics for agencies."""
        for agency in queryset:
            stats = agency.update_commission_stats()
            messages.info(
                request,
                f"{agency.name}: {stats['agents_count']} agents, "
                f"${stats['unpaid']:,.2f} unpaid, ${stats['pending']:,.2f} pending, "
                f"${stats['paid']:,.2f} paid",
            )

    update_commission_stats.short_description = "Update commission stats"


# =============================================
# AGENCY COMMISSION PAYOUT ADMIN
# =============================================


@admin.register(AgencyCommissionPayout)
class AgencyCommissionPayoutAdmin(DispatcherAdminMixin, admin.ModelAdmin):
    """Admin for agency commission payouts - hidden from dispatchers."""
    
    def has_module_permission(self, request):
        """Only show to superusers."""
        return request.user.is_superuser
    
    def has_view_permission(self, request, obj=None):
        """Only superusers can view."""
        return request.user.is_superuser
    
    def has_add_permission(self, request):
        """Only superusers can add."""
        return request.user.is_superuser
    
    def has_change_permission(self, request, obj=None):
        """Only superusers can change."""
        return request.user.is_superuser
    
    def has_delete_permission(self, request, obj=None):
        """Only superusers can delete."""
        return request.user.is_superuser
    """Admin interface for agency commission payouts."""

    list_display = [
        "payout_id",
        "agency_link",
        "total_amount",
        "agent_count",
        "payout_period",
        "paid_at",
        "payment_status",
    ]
    list_filter = ["paid_at", "payout_period_start", "agency"]
    search_fields = ["agency__name"]
    inlines = [AgentPayoutInline]
    readonly_fields = ["paid_at", "agent_payout_details"]
    list_per_page = 50

    @admin.action(description="Send agency commission statement email")
    def send_commission_statement(self, request, queryset):
        from users.emails import send_agency_commission_statement

        sent = 0
        failed = 0
        for payout in queryset.select_related("agency"):
            agency = payout.agency
            # Send to all agency heads
            head_emails = list(agency.heads.values_list("email", flat=True))
            if not head_emails:
                failed += 1
                continue
            payout_sent = False
            for email in head_emails:
                if email and send_agency_commission_statement(agency=agency, payout=payout, recipient_email=email):
                    payout_sent = True
            if payout_sent:
                sent += 1
            else:
                failed += 1

        if sent:
            self.message_user(request, f"Sent {sent} agency statement(s) successfully.")
        if failed:
            self.message_user(request, f"Failed to send {failed} statement(s).", level="error")

    fieldsets = (
        (
            "Payout Information",
            {
                "fields": (
                    "agency",
                    "total_amount",
                    "payout_period_start",
                    "payout_period_end",
                )
            },
        ),
        ("Payment Details", {"fields": ("paid_at", "notes")}),
        (
            "Agent Payouts",
            {"fields": ("agent_payout_details",), "classes": ("collapse",)},
        ),
    )

    def get_queryset(self, request):
        """Optimize queryset with related data."""
        return (
            super()
            .get_queryset(request)
            .select_related("agency")
            .prefetch_related("agent_payouts")
        )

    def payout_id(self, obj):
        """Display payout ID with # prefix."""
        return f"#{obj.id}"

    payout_id.short_description = "ID"

    def agency_link(self, obj):
        """Display agency name as admin link."""
        return create_admin_link("agency", obj.agency.id, obj.agency.name)

    agency_link.short_description = "Agency"

    def agent_count(self, obj):
        """Display count of agent payouts."""
        return obj.agent_payouts.count()

    agent_count.short_description = "Agents"

    def payout_period(self, obj):
        """Display formatted payout period."""
        return format_html(
            "{} to {}",
            obj.payout_period_start.strftime("%b %d, %Y"),
            obj.payout_period_end.strftime("%b %d, %Y"),
        )

    payout_period.short_description = "Period"

    def payment_status(self, obj):
        """Display payment status with color coding."""
        return format_html('<span style="color: green;">✓ Paid</span>')

    payment_status.short_description = "Status"

    def agent_payout_details(self, obj):
        """Display detailed agent payout table."""
        payouts = obj.agent_payouts.all()
        if not payouts:
            return "No agent payouts"

        html = "<table style='width: 100%; border-collapse: collapse;'>"
        html += "<tr style='background-color: #f0f0f0;'>"
        html += "<th style='padding: 4px; border: 1px solid #ddd;'>ID</th>"
        html += "<th style='padding: 4px; border: 1px solid #ddd;'>Agent</th>"
        html += "<th style='padding: 4px; border: 1px solid #ddd;'>Amount</th>"
        html += "<th style='padding: 4px; border: 1px solid #ddd;'>Trips</th>"
        html += "<th style='padding: 4px; border: 1px solid #ddd;'>Period</th>"
        html += "</tr>"

        for payout in payouts:
            payout_link = create_admin_link(
                "commissionpayout", payout.id, f"#{payout.id}"
            )
            html += (
                f"<tr>"
                f"<td style='padding: 4px; border: 1px solid #ddd;'>{payout_link}</td>"
                f"<td style='padding: 4px; border: 1px solid #ddd;'>{payout.agent}</td>"
                f"<td style='padding: 4px; border: 1px solid #ddd;'>${payout.total_amount:,.2f}</td>"
                f"<td style='padding: 4px; border: 1px solid #ddd;'>{payout.reservations.count()}</td>"
                f"<td style='padding: 4px; border: 1px solid #ddd;'>"
                f"{payout.payout_period_start.strftime('%b %d')} - {payout.payout_period_end.strftime('%b %d, %Y')}"
                f"</td>"
                f"</tr>"
            )

        html += "</table>"
        return mark_safe(html)

    agent_payout_details.short_description = "Agent Payout Details"

    actions = ["cancel_agency_payouts", "send_commission_statement"]

    def cancel_agency_payouts(self, request, queryset):
        """Cancel selected agency payouts."""
        cancelled_count = 0
        total_cancelled = 0

        for payout in queryset:
            total_cancelled += payout.total_amount
            logger.info(
                f"Cancelling agency payout #{payout.id} for ${payout.total_amount}"
            )
            payout.delete()  # Pre-delete signal handles cleanup
            cancelled_count += 1

        if cancelled_count:
            messages.success(
                request,
                f"Cancelled {cancelled_count} agency payouts totaling ${total_cancelled:.2f}.",
            )

    cancel_agency_payouts.short_description = "Cancel agency payouts"


# =============================================
# CONTACT US FORM ADMIN
# =============================================


@admin.register(ContactUsForm)
class ContactUsFormAdmin(admin.ModelAdmin):
    """Admin interface for contact form submissions with status management."""

    list_display = [
        "id",
        "full_name",
        "email",
        "phone_number",
        "contact_method",
        "status",
        "status_display",
        "contacted_at",
        "created_at",
    ]
    list_filter = ["status", "contact_method", "created_at", "contacted_at"]
    search_fields = [
        "first_name",
        "last_name",
        "email",
        "phone_number",
        "about",
    ]
    list_editable = ["status"]
    list_per_page = 50
    readonly_fields = ["created_at", "contacted_at"]
    date_hierarchy = "created_at"

    fieldsets = (
        (
            "Contact Information",
            {
                "fields": (
                    "first_name",
                    "last_name",
                    "email",
                    "phone_number",
                    "contact_method",
                )
            },
        ),
        ("Message", {"fields": ("about",)}),
        ("Status", {"fields": ("status", "contacted_at")}),
        ("System Information", {"fields": ("created_at",), "classes": ("collapse",)}),
    )

    def full_name(self, obj):
        """Display full name."""
        return f"{obj.first_name} {obj.last_name}"

    full_name.short_description = "Name"

    def status_display(self, obj):
        """Display status with color coding."""
        colors = {
            "pending": "orange",
            "contacted": "blue",
            "closed": "green",
        }
        color = colors.get(obj.status, "gray")
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            obj.get_status_display(),
        )

    status_display.short_description = "Status"

    actions = [
        "mark_as_contacted",
        "mark_as_closed",
        "mark_as_pending",
    ]

    def mark_as_contacted(self, request, queryset):
        """Mark selected forms as contacted and set contacted_at timestamp."""
        from django.utils import timezone
        
        now = timezone.now()
        updated = 0
        for form in queryset:
            form.status = "contacted"
            if not form.contacted_at:  # Only set if not already set
                form.contacted_at = now
            form.save()
            updated += 1
        
        self.message_user(
            request,
            f"Successfully marked {updated} form(s) as contacted.",
            messages.SUCCESS,
        )

    mark_as_contacted.short_description = "Mark selected as contacted"

    def mark_as_closed(self, request, queryset):
        """Mark selected forms as closed."""
        updated = queryset.update(status="closed")
        self.message_user(
            request,
            f"Successfully marked {updated} form(s) as closed.",
            messages.SUCCESS,
        )

    mark_as_closed.short_description = "Mark selected as closed"

    def mark_as_pending(self, request, queryset):
        """Mark selected forms as pending."""
        updated = queryset.update(status="pending")
        self.message_user(
            request,
            f"Successfully marked {updated} form(s) as pending.",
            messages.SUCCESS,
        )

    mark_as_pending.short_description = "Mark selected as pending"


# =============================================
# ADMIN SITE CUSTOMIZATION
# =============================================

# Customize admin site headers
admin.site.site_header = "Travel Management Admin"
admin.site.site_title = "Travel Admin"
admin.site.index_title = "Travel Management Administration"
