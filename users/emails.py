from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
import logging
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from reservations.models import Reservation
from django.shortcuts import get_object_or_404
from django.utils import timezone
import logging
from django.http import JsonResponse
from django.views.decorators.http import require_POST
import json
import threading
import time

logger = logging.getLogger(__name__)


def log_email_sent(email_type, recipient_email, subject="", sent_by=None,
                   reservation=None, success=True, metadata=None):
    """Log an email send to the EmailLog model for staff metrics tracking."""
    try:
        from ops.models import EmailLog
        EmailLog.objects.create(
            email_type=email_type,
            sent_by=sent_by,
            recipient_email=recipient_email,
            subject=subject,
            reservation=reservation,
            success=success,
            metadata=metadata or {},
        )
    except Exception as e:
        logger.warning(f"Failed to log email send: {e}")


def _send_email_with_retry(email_func, max_retries=3):
    """Send email with retry logic in background thread"""
    def background_send():
        for attempt in range(max_retries):
            try:
                email_func()
                logger.info(f"Email sent successfully on attempt {attempt + 1}")
                return  # Success, exit retry loop
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    logger.warning(f"Email attempt {attempt + 1} failed, retrying in {wait_time}s: {e}")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Email failed after {max_retries} attempts: {e}")
    
    thread = threading.Thread(target=background_send)
    thread.daemon = True
    thread.start()


@login_required
@require_POST
def send_reservation_confirmation_ajax(request):
    """
    AJAX endpoint to send reservation confirmation email
    """
    try:
        data = json.loads(request.body)
        reservation_id = data.get("reservation_id")
        reservation = get_object_or_404(Reservation, uuid=reservation_id)
        
        # Check permissions - staff or superuser
        if not request.user.is_staff:
            return JsonResponse({"success": False, "error": "Permission denied"})
        
        send_reservation_confirmation(reservation, sent_by=request.user)

        return JsonResponse({"success": True})
    
    except Exception as e:
        logger.error(f"Error sending confirmation email: {e}")
        return JsonResponse({"success": False, "error": str(e)})


def send_reservation_confirmation(reservation, sent_by=None):
    """This Reservation is Called in the View
    When a Reservation is created with the reservation Object
    Renders a nicely formatted HTML and emails a Confirmation"""
    logger.info(
        f"Preparing to send reservation confirmation for {reservation.customer}"
    )
    subject = "Thank you for booking with Grayson Towncar!"

    def _send_email():
        try:
            legs = reservation.legs.all()
            # Check if any leg is a return trip
            has_return_trip = any(leg.get_trip_type() == 'return' for leg in legs)

            context = {
                "reservation": reservation,
                "legs": legs,
                "date": timezone.localdate(),
                "has_return_trip": has_return_trip,
            }

            from_email = "reservations@graysontowncar.com"
            to = [reservation.customer.email]
            html_content = render_to_string("users/confirmation_email.html", context)

            msg = EmailMultiAlternatives(subject, "", from_email, to)
            msg.attach_alternative(html_content, "text/html")
            msg.send()

            logger.info(
                f"Confirmation email sent successfully for reservation {reservation.uuid}"
            )
            log_email_sent(
                email_type="confirmation",
                recipient_email=reservation.customer.email,
                subject=subject,
                sent_by=sent_by,
                reservation=reservation,
            )

        except Exception as e:
            logger.exception(
                f"Error sending confirmation email for reservation {reservation.uuid}: {e}"
            )
            raise  # Re-raise for retry logic

    # Send with retry in background thread
    _send_email_with_retry(_send_email, max_retries=3)


def send_afterhours_fee_notice(reservation, leg, amount, sent_by=None):
    """Notify the customer that an after-hours service fee was applied because the
    pickup falls in the late-night window (10 PM-6 AM) — typically after a flight
    delay moved the arrival past 10 PM. Professional, automated-style notice.
    Runs in a background thread with retry."""
    logger.info(
        f"Preparing to send after-hours fee notice for reservation {reservation.id} leg {leg.id}"
    )
    subject = f"Update to your Grayson Towncar reservation #{reservation.id}"

    def _send_email():
        try:
            context = {
                "reservation": reservation,
                "leg": leg,
                "amount": amount,
                "date": timezone.localdate(),
            }
            from_email = "reservations@graysontowncar.com"
            to = [reservation.customer.email]
            html_content = render_to_string("users/afterhours_fee_notice.html", context)

            msg = EmailMultiAlternatives(subject, "", from_email, to)
            msg.attach_alternative(html_content, "text/html")
            msg.send()

            logger.info(
                f"After-hours fee notice sent for reservation {reservation.uuid} leg {leg.id}"
            )
            log_email_sent(
                email_type="afterhours_fee",
                recipient_email=reservation.customer.email,
                subject=subject,
                sent_by=sent_by,
                reservation=reservation,
            )
        except Exception as e:
            logger.exception(
                f"Error sending after-hours fee notice for reservation {reservation.uuid}: {e}"
            )
            raise

    _send_email_with_retry(_send_email, max_retries=3)


def send_reservation_confirmation_custom_recipient(reservation, recipient_email, sender_name=None, sent_by=None):
    """
    Send reservation confirmation email to a custom recipient.
    Returns True if the send is queued successfully, False otherwise.
    """
    if not recipient_email:
        logger.error("Custom confirmation email failed: missing recipient email")
        return False

    try:
        validate_email(recipient_email)
    except ValidationError:
        logger.error(f"Custom confirmation email failed: invalid recipient email {recipient_email}")
        return False

    try:
        legs = reservation.legs.all()
        has_return_trip = any(leg.get_trip_type() == 'return' for leg in legs)

        context = {
            "reservation": reservation,
            "legs": legs,
            "date": timezone.localdate(),
            "has_return_trip": has_return_trip,
            "sender_name": sender_name,
            "recipient_email": recipient_email,
        }

        subject = "Grayson Towncar Reservation Confirmation"
        from_email = "reservations@graysontowncar.com"
        to = [recipient_email]
        html_content = render_to_string("users/confirmation_email.html", context)

        def _send_email():
            try:
                msg = EmailMultiAlternatives(subject, "", from_email, to)
                msg.attach_alternative(html_content, "text/html")
                msg.send()
                logger.info(
                    f"Custom confirmation email sent for reservation {reservation.uuid} to {recipient_email}"
                )
                log_email_sent(
                    email_type="confirmation",
                    recipient_email=recipient_email,
                    subject=subject,
                    sent_by=sent_by,
                    reservation=reservation,
                )
            except Exception as e:
                logger.exception(
                    f"Error sending custom confirmation email for reservation {reservation.uuid}: {e}"
                )
                raise

        _send_email_with_retry(_send_email, max_retries=3)
        return True
    except Exception as e:
        logger.exception(f"Failed to queue custom confirmation email: {e}")
        return False


# Subject line per reminder stage. "manual" is what the staff "Send reminder"
# button uses; the four automation stages match ops.unpaid_reminders.
PAYMENT_REMINDER_SUBJECTS = {
    "manual": "Action Required: Finalize Your Grayson Towncar Reservation #{id}",
    "first": "Finalize your Grayson Towncar reservation #{id}",
    "second": "Reminder: your Grayson Towncar reservation #{id} is still unpaid",
    "three_day": "3 days to pickup — payment needed: reservation #{id}",
    "final": "Final reminder: complete payment for reservation #{id}",
}


def _log_payment_reminder_sent(
    reservation, recipient, subject, stage="manual", sent_by=None, automated=False
):
    """
    Centralized post-send logging for payment reminders.

    Writes an EmailLog row tagged with stage + automated flag, then logs a
    CommunicationAttempt against the open PAYMENT_CHASE task (if any).
    Used by both the staff AJAX path and the automated reminder engine.
    """
    metadata = {"stage": stage, "automated": automated}
    log_email_sent(
        email_type="payment_reminder",
        recipient_email=recipient,
        subject=subject,
        sent_by=sent_by,
        reservation=reservation,
        success=True,
        metadata=metadata,
    )

    try:
        from ops.models import OperationalTask, CommunicationAttempt
        from ops.services import log_communication

        payment_task = OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            reservation=reservation,
            status__in=list(OperationalTask.OPEN_STATUSES),
        ).first()
        if not payment_task:
            return

        notes = f"Payment reminder email sent (${reservation.amount_owed} owed, stage={stage})"
        if sent_by is not None:
            # Staff-triggered — also writes StaffActivity via log_communication.
            log_communication(
                task=payment_task,
                channel="email",
                outcome="sent",
                user=sent_by,
                contact_value=recipient,
                notes=notes,
                metadata=metadata,
            )
        else:
            # Automated — log the CommunicationAttempt directly so we don't
            # need a User row, and skip the StaffActivity entry which is
            # meant for human actions only.
            CommunicationAttempt.objects.create(
                task=payment_task,
                channel="email",
                outcome="sent",
                staff_user=None,
                contact_value=recipient,
                notes=notes,
                metadata=metadata,
            )
            payment_task.attempts += 1
            payment_task.last_attempt_at = timezone.now()
            payment_task.save(update_fields=["attempts", "last_attempt_at", "updated_at"])
    except Exception as e:
        logger.warning(f"Failed to log comm for payment reminder: {e}")


def send_payment_reminder(
    reservation, checkout_url, stage="manual", sent_by=None, automated=False
):
    """
    Synchronously send a payment reminder email and log the send.

    Stage selects the subject line and the per-stage copy block in the
    template (via the ``reminder_stage`` context variable). Raises on failure
    so callers (AJAX endpoint, reminder engine) can react atomically.
    """
    recipient = reservation.customer.email if reservation.customer else None
    if not recipient:
        raise ValueError(
            f"Reservation {reservation.uuid} has no customer email — cannot send reminder"
        )

    subject = PAYMENT_REMINDER_SUBJECTS.get(stage, PAYMENT_REMINDER_SUBJECTS["manual"]).format(
        id=reservation.id
    )

    context = {
        "reservation": reservation,
        "checkout_url": checkout_url,
        "reminder_stage": stage,
    }
    html_content = render_to_string("users/payment_reminder_email.html", context)

    msg = EmailMultiAlternatives(
        subject, "", "reservations@graysontowncar.com", [recipient]
    )
    msg.attach_alternative(html_content, "text/html")
    msg.send()

    logger.info(
        f"Payment reminder sent for reservation {reservation.uuid} "
        f"to {recipient} (stage={stage}, automated={automated})"
    )
    _log_payment_reminder_sent(
        reservation=reservation,
        recipient=recipient,
        subject=subject,
        stage=stage,
        sent_by=sent_by,
        automated=automated,
    )


@login_required
@require_POST
def send_payment_reminder_ajax(request):
    """
    AJAX endpoint to send a payment reminder email with a checkout link.
    Sends synchronously so failures are reported back to the caller.
    """
    try:
        data = json.loads(request.body)
        reservation_id = data.get("reservation_id")
        reservation = get_object_or_404(Reservation, uuid=reservation_id)

        if not request.user.is_staff:
            return JsonResponse({"success": False, "error": "Permission denied"})

        from django.urls import reverse
        checkout_url = request.build_absolute_uri(
            reverse("create_checkout_session", args=[str(reservation.uuid)])
        )

        send_payment_reminder(
            reservation=reservation,
            checkout_url=checkout_url,
            stage="manual",
            sent_by=request.user,
            automated=False,
        )

        return JsonResponse({"success": True, "email": reservation.customer.email})

    except Exception as e:
        logger.exception(f"Error sending payment reminder: {e}")
        return JsonResponse({"success": False, "error": str(e)})


def send_refund_request_notification(refund_request_or_reservation):
    """
    Send email notification to admin when a refund is requested.
    Accepts either a RefundRequest object (new system) or a Reservation (legacy compat).
    """
    # Support both RefundRequest and Reservation objects
    from reservations.models import RefundRequest
    if isinstance(refund_request_or_reservation, RefundRequest):
        rr = refund_request_or_reservation
        reservation = rr.reservation
        requested_by = rr.requested_by.get_full_name() if rr.requested_by else "Unknown"
        requested_at = rr.requested_at
        refund_reason = rr.reason
        refund_amount = rr.amount
        refund_type = rr.get_refund_type_display()
        suggested_amount = rr.suggested_amount
    else:
        reservation = refund_request_or_reservation
        requested_by = reservation.refund_requested_by.get_full_name() if reservation.refund_requested_by else "Unknown"
        requested_at = reservation.refund_requested_at
        refund_reason = reservation.refund_reason
        refund_amount = reservation.refund_amount
        refund_type = "Full Cancellation"
        suggested_amount = None

    logger.info(f"Preparing to send refund request notification for reservation {reservation.id}")

    def _send_email():
        try:
            context = {
                "reservation": reservation,
                "requested_by": requested_by,
                "requested_at": requested_at,
                "refund_reason": refund_reason,
                "refund_amount": refund_amount,
                "refund_type": refund_type,
                "suggested_amount": suggested_amount,
                "total_paid": reservation.total_paid,
                "admin_url": "https://www.graysontowncar.com/dispatching/refund-management/",
            }

            subject = f"Refund Requested - Reservation #{reservation.id} ({refund_type})"
            from_email = "reservations@graysontowncar.com"
            to = ["admin@graysontowncar.com"]

            html_content = render_to_string("users/refund_request_email.html", context)

            msg = EmailMultiAlternatives(subject, "", from_email, to)
            msg.attach_alternative(html_content, "text/html")
            msg.send()

            logger.info(f"Refund request notification sent successfully for reservation {reservation.id}")

        except Exception as e:
            logger.exception(f"Error sending refund request notification: {e}")
            raise

    # Send with retry in background thread
    _send_email_with_retry(_send_email, max_retries=3)


def agent_register_email(instance):
    """
    Sends a welcome email to new travel agents after registration.
    Uses background thread to avoid blocking the request.
    """
    try:
        context = {
            "agent": instance,
            "agent_name": instance.agent_name or instance.user.get_full_name() or instance.user.username,
            "email": instance.user.email,
        }
        subject = "Welcome to Grayson Towncar Travel Agent Portal!"
        from_email = "reservations@graysontowncar.com"
        to = [instance.user.email]
        html_content = render_to_string("users/agent_register_email.html", context)

        def _send_email():
            msg = EmailMultiAlternatives(subject, "", from_email, to)
            msg.attach_alternative(html_content, "text/html")
            msg.send()
            logger.info(f"Welcome email sent to {instance.user.email}")

        _send_email_with_retry(_send_email, max_retries=3)

    except Exception as e:
        logger.error(f"Error sending agent welcome email: {e}")


def send_internal_confirmation(reservation):
    """Emails Self when a reservation gets made in case of any errors and customer does not get an email"""
    logger.info(
        f"Preparing to send internal confirmation email for {reservation.customer}"
    )

    def _send_email():
        try:
            context = {
                "reservation": reservation,
                "legs": reservation.legs.all(),
                "date": timezone.localdate(),
            }

            subject = "Reservation Submission"
            from_email = "reservations@graysontowncar.com"
            to = ["reservations@graysontowncar.com"]
            logger.info(f"Email subject: {subject}")
            logger.info(f"Sending to: {to}")
            html_content = render_to_string("users/confirmation_email.html", context)
            logger.info("HTML content rendered successfully")

            msg = EmailMultiAlternatives(subject, "", from_email, to)
            msg.attach_alternative(html_content, "text/html")
            msg.send()

            logger.info(
                f"Internal confirmation email sent successfully for reservation {reservation.uuid}"
            )

        except Exception as e:
            logger.exception(
                f"Error sending internal confirmation email for reservation {reservation.uuid}: {e}"
            )
            raise  # Re-raise for retry logic

    # Send with retry in background thread
    _send_email_with_retry(_send_email, max_retries=3)


def send_driver_payment_statement(driver, payment, legs, recipient_email, sent_by=None):
    """
    Send a driver payment statement email.
    Returns True if queued successfully, False otherwise.
    """
    if not recipient_email:
        logger.error("Driver payment statement failed: missing recipient email")
        return False

    try:
        validate_email(recipient_email)
    except ValidationError:
        logger.error(f"Driver payment statement failed: invalid recipient email {recipient_email}")
        return False

    try:
        pay_period_start = None
        pay_period_end = None
        if legs:
            leg_dates = [leg.pickup_date for leg in legs if leg.pickup_date]
            if leg_dates:
                pay_period_start = min(leg_dates)
                pay_period_end = max(leg_dates)

        # Get leg payments for breakdown display with related data
        from drivers.models import LegPayment
        leg_payments = LegPayment.objects.filter(
            payment=payment
        ).select_related(
            "leg",
            "leg__reservation",
            "leg__reservation__customer"
        ).order_by("leg__pickup_date", "leg__pickup_time")

        context = {
            "driver": driver,
            "payment": payment,
            "legs": legs,
            "leg_payments": leg_payments,
            "date": timezone.localdate(),
            "pay_period_start": pay_period_start,
            "pay_period_end": pay_period_end,
        }

        if pay_period_start and pay_period_end:
            subject = (
                f"Grayson Towncar - Payment Statement "
                f"{pay_period_start.strftime('%b %d, %Y')} - {pay_period_end.strftime('%b %d, %Y')}"
            )
        else:
            subject = f"Grayson Towncar - Payment Statement {timezone.now().strftime('%b %d, %Y')}"
        from_email = "reservations@graysontowncar.com"
        to = [recipient_email]
        html_content = render_to_string("users/driver_payment_statement.html", context)

        def _send_email():
            try:
                msg = EmailMultiAlternatives(subject, "", from_email, to)
                msg.attach_alternative(html_content, "text/html")
                msg.send()
                logger.info(
                    f"Driver payment statement sent to {recipient_email} for payment {payment.id}"
                )
                log_email_sent(
                    email_type="driver_statement",
                    recipient_email=recipient_email,
                    subject=subject,
                    sent_by=sent_by,
                    metadata={"driver_id": driver.id, "payment_id": payment.id},
                )
            except Exception as e:
                logger.exception(
                    f"Error sending driver payment statement for payment {payment.id}: {e}"
                )
                raise

        _send_email_with_retry(_send_email, max_retries=3)
        return True
    except Exception as e:
        logger.exception(f"Failed to queue driver payment statement email: {e}")
        return False


def thankyou_email(instance):
    """
    Branded thank-you email for ContactUsForm submitters.

    ContactUsForm uses first_name/last_name (no .name); we fall back via
    getattr so a future model with a single name field still works.
    """
    try:
        display_name = (
            getattr(instance, "name", None)
            or " ".join(p for p in [
                getattr(instance, "first_name", "") or "",
                getattr(instance, "last_name", "") or "",
            ] if p).strip()
        )
        context = {"name": display_name, "email": instance.email}
        subject = "Thank you for contacting Grayson Towncar!"
        from_email = "reservations@graysontowncar.com"
        to = [instance.email]
        html_content = render_to_string("users/thankyou_email.html", context)

        def _send_email():
            msg = EmailMultiAlternatives(subject, "", from_email, to)
            msg.attach_alternative(html_content, "text/html")
            msg.send()
            logger.info(f"Thank you email sent to {instance.email}")

        _send_email_with_retry(_send_email, max_retries=3)

    except Exception as e:
        logger.error(f"Error sending thank you email: {e}")


def partner_inquiry_thankyou_email(instance):
    """
    Sends a branded "we received your partner inquiry" email to the submitter.
    Uses the existing partner_contact_email.html template (Tabular-built).
    """
    try:
        context = {
            "name": instance.name,
            "email": instance.email,
            "agency_name": instance.agency_name,
        }
        subject = "We received your partner inquiry — Grayson Towncar"
        from_email = "reservations@graysontowncar.com"
        to = [instance.email]
        html_content = render_to_string("users/partner_contact_email.html", context)

        def _send_email():
            msg = EmailMultiAlternatives(subject, "", from_email, to)
            msg.attach_alternative(html_content, "text/html")
            msg.send()
            logger.info(f"Partner inquiry thank-you sent to {instance.email}")

        _send_email_with_retry(_send_email, max_retries=3)

    except Exception as e:
        logger.error(f"Error sending partner inquiry thank-you email: {e}")


def partner_inquiry_admin_notification(instance):
    """
    Notify staff that a new partner inquiry was submitted, so it doesn't sit
    silently in the DB. Sent to admin@graysontowncar.com (same inbox used by
    refund-request notifications).
    """
    try:
        context = {
            "inquiry": instance,
            # Best-effort deep link into the Django admin change page so a
            # staff member can open it from the email with one click.
            "admin_url": f"https://www.graysontowncar.com/admin/users/partnerform/{instance.pk}/change/",
        }
        subject = f"New partner inquiry: {instance.name} ({instance.agency_name})"
        from_email = "reservations@graysontowncar.com"
        to = ["admin@graysontowncar.com"]
        html_content = render_to_string("users/partner_inquiry_admin_email.html", context)

        def _send_email():
            msg = EmailMultiAlternatives(subject, "", from_email, to)
            msg.attach_alternative(html_content, "text/html")
            msg.send()
            logger.info(f"Partner inquiry admin notification sent for #{instance.pk}")

        _send_email_with_retry(_send_email, max_retries=3)

    except Exception as e:
        logger.error(f"Error sending partner inquiry admin notification: {e}")


def send_agent_commission_statement(agent, payout, recipient_email, sent_by=None):
    """
    Send a commission statement email to a travel agent.
    Returns True if queued successfully, False otherwise.
    """
    if not recipient_email:
        logger.error("Agent commission statement failed: missing recipient email")
        return False

    try:
        validate_email(recipient_email)
    except ValidationError:
        logger.error(f"Agent commission statement failed: invalid recipient email {recipient_email}")
        return False

    try:
        # Get reservations for this payout
        reservations = payout.reservations.all().order_by("created_at").select_related("customer")

        # Calculate totals
        total_base_price = sum(r.base_price or 0 for r in reservations)
        total_gratuity = sum(r.gratuity_amount or 0 for r in reservations)
        total_additional = sum(r.additional_charges or 0 for r in reservations)
        total_charged = total_base_price + total_gratuity + total_additional

        context = {
            "agent": agent,
            "payout": payout,
            "reservations": reservations,
            "total_base_price": total_base_price,
            "total_gratuity": total_gratuity,
            "total_additional": total_additional,
            "total_charged": total_charged,
            "date": timezone.localdate(),
        }

        subject = (
            f"Grayson Towncar - Commission Statement "
            f"{payout.payout_period_start.strftime('%b %d, %Y')} - {payout.payout_period_end.strftime('%b %d, %Y')}"
        )
        from_email = "reservations@graysontowncar.com"
        to = [recipient_email]
        html_content = render_to_string("users/agent_commission_statement.html", context)

        def _send_email():
            try:
                msg = EmailMultiAlternatives(subject, "", from_email, to)
                msg.attach_alternative(html_content, "text/html")
                msg.send()
                logger.info(
                    f"Agent commission statement sent to {recipient_email} for payout {payout.id}"
                )
                log_email_sent(
                    email_type="agent_commission",
                    recipient_email=recipient_email,
                    subject=subject,
                    sent_by=sent_by,
                    metadata={"agent_id": agent.id, "payout_id": payout.id},
                )
            except Exception as e:
                logger.exception(
                    f"Error sending agent commission statement for payout {payout.id}: {e}"
                )
                raise

        _send_email_with_retry(_send_email, max_retries=3)
        return True
    except Exception as e:
        logger.exception(f"Failed to queue agent commission statement email: {e}")
        return False


def send_agency_commission_statement(agency, payout, recipient_email, sent_by=None):
    """
    Send a commission statement email to a travel agency.
    Returns True if queued successfully, False otherwise.
    """
    if not recipient_email:
        logger.error("Agency commission statement failed: missing recipient email")
        return False

    try:
        validate_email(recipient_email)
    except ValidationError:
        logger.error(f"Agency commission statement failed: invalid recipient email {recipient_email}")
        return False

    try:
        # Get agent payouts for this agency payout
        agent_payouts = payout.agent_payouts.all().select_related("agent", "agent__user")

        # Calculate totals
        total_agents = agent_payouts.count()
        total_reservations = sum(ap.reservations.count() for ap in agent_payouts)
        average_commission = payout.total_amount / total_agents if total_agents > 0 else 0

        context = {
            "agency": agency,
            "payout": payout,
            "agent_payouts": agent_payouts,
            "total_agents": total_agents,
            "total_reservations": total_reservations,
            "average_commission": average_commission,
            "date": timezone.localdate(),
        }

        subject = (
            f"Grayson Towncar - Agency Commission Statement "
            f"{payout.payout_period_start.strftime('%b %d, %Y')} - {payout.payout_period_end.strftime('%b %d, %Y')}"
        )
        from_email = "reservations@graysontowncar.com"
        to = [recipient_email]
        html_content = render_to_string("users/agency_commission_statement.html", context)

        def _send_email():
            try:
                msg = EmailMultiAlternatives(subject, "", from_email, to)
                msg.attach_alternative(html_content, "text/html")
                msg.send()
                logger.info(
                    f"Agency commission statement sent to {recipient_email} for payout {payout.id}"
                )
                log_email_sent(
                    email_type="agency_commission",
                    recipient_email=recipient_email,
                    subject=subject,
                    sent_by=sent_by,
                    metadata={"agency_id": agency.id, "payout_id": payout.id},
                )
            except Exception as e:
                logger.exception(
                    f"Error sending agency commission statement for payout {payout.id}: {e}"
                )
                raise

        _send_email_with_retry(_send_email, max_retries=3)
        return True
    except Exception as e:
        logger.exception(f"Failed to queue agency commission statement email: {e}")
        return False


def send_lead_quote_email(lead, booking_url=None):
    """
    Send a quote email to a lead. Used as fallback when SMS fails
    (e.g. UK numbers, invalid phone numbers).

    Args:
        lead: Lead instance with email, trip details, etc.
        booking_url: Optional URL to the booking page

    Returns:
        bool: True if email was queued successfully, False otherwise
    """
    if not lead.email:
        logger.warning(f"Lead #{lead.id} has no email address, cannot send quote email")
        return False

    try:
        validate_email(lead.email)
    except ValidationError:
        logger.warning(f"Lead #{lead.id} has invalid email: {lead.email}")
        return False

    try:
        # Get the latest quote for this lead
        quote = lead.quotes.filter(is_current=True).select_related("vehicle").first()

        # Build a direct booking URL from the quote's route + vehicle
        if not booking_url and quote and quote.vehicle:
            try:
                from rates.models import Rate
                rate = Rate.objects.filter(
                    vehicle=quote.vehicle,
                    route__origin__name__iexact=quote.pickup_location,
                    route__destination__name__iexact=quote.dropoff_location,
                ).first()
                if not rate:
                    # Try reversed direction
                    rate = Rate.objects.filter(
                        vehicle=quote.vehicle,
                        route__origin__name__iexact=quote.dropoff_location,
                        route__destination__name__iexact=quote.pickup_location,
                    ).first()
                if rate:
                    booking_url = f"https://graysontowncar.com/book-orlando-transportation/{rate.pk}"
            except Exception:
                logger.debug(f"Could not resolve booking rate for lead #{lead.id}")

        context = {
            "lead": lead,
            "quote": quote,
            "booking_url": booking_url or "https://graysontowncar.com/rates-booking/",
        }

        subject = f"Your Grayson Towncar Quote — {lead.pickup_location or 'Orlando'} to {lead.dropoff_location or 'your destination'}"
        from_email = "reservations@graysontowncar.com"
        to = [lead.email]
        html_content = render_to_string("users/lead_quote_email.html", context)

        def _send_email():
            msg = EmailMultiAlternatives(subject, "", from_email, to)
            msg.attach_alternative(html_content, "text/html")
            msg.send()
            logger.info(f"Lead quote email sent to {lead.email} for lead #{lead.id}")

        _send_email_with_retry(_send_email, max_retries=3)
        return True
    except Exception as e:
        logger.exception(f"Failed to send lead quote email for lead #{lead.id}: {e}")
        return False
