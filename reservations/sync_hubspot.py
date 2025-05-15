from django.http import JsonResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.csrf import csrf_exempt
from reservations.models import Reservation, Customer
import logging
import time
import os
from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInput as ContactInput

# Import the entire module instead of specific functions
import reservations.hubspot_service as hubspot_service

logger = logging.getLogger(__name__)

@csrf_exempt
@staff_member_required
def sync_hubspot_view(request):
    """Admin-only endpoint to sync data to HubSpot"""
    
    # Get parameters from request
    limit = request.GET.get('limit')
    if limit:
        limit = int(limit)
    
    start_id = request.GET.get('start_id')
    if start_id:
        start_id = int(start_id)
    else:
        start_id = 0
    
    dry_run = request.GET.get('dry_run') == 'true'
    sync_type = request.GET.get('type', 'reservations')  # 'reservations', 'customers', or 'all'
    
    results = {
        'success': True,
        'total': 0,
        'success_count': 0,
        'error_count': 0,
        'results': [],
        'next_start_id': start_id
    }
    
    # Sync customers if requested
    if sync_type in ['customers', 'all']:
        customer_results = sync_customers(start_id, limit, dry_run)
        results['customers'] = customer_results
        
    # Sync reservations if requested
    if sync_type in ['reservations', 'all']:
        reservation_results = sync_reservations(start_id, limit, dry_run)
        results['reservations'] = reservation_results
        
    return JsonResponse(results)

def sync_customers(start_id=0, limit=None, dry_run=False):
    """Sync customers to HubSpot"""
    query = Customer.objects.filter(id__gt=start_id).order_by('id')
    if limit:
        query = query.all()[:limit]
    
    total = query.count()
    results = []
    success_count = 0
    error_count = 0
    
    HUBSPOT_TOKEN = os.environ.get('HUBSPOT_TOKEN')
    if not HUBSPOT_TOKEN:
        return {
            'success': False,
            'error': 'HUBSPOT_TOKEN not found',
            'total': total,
            'success_count': 0,
            'error_count': total
        }
    
    client = HubSpot(access_token=HUBSPOT_TOKEN)
    
    for customer in query:
        result_entry = {
            'customer_id': customer.id,
            'name': customer.get_full_name()
        }
        
        try:
            if not dry_run:
                # Search for existing contact
                body = {
                    "filterGroups": [
                        {
                            "filters": [
                                {"propertyName": "email", "operator": "EQ", "value": customer.email}
                            ]
                        }
                    ]
                }
                
                try:
                    resp = client.crm.contacts.search_api.do_search(
                        public_object_search_request=body
                    )
                    if resp.results:
                        contact_id = resp.results[0].id
                        result_entry['status'] = 'existing'
                        result_entry['contact_id'] = contact_id
                    else:
                        # Create new contact
                        props = {
                            "email": customer.email,
                            "firstname": customer.first_name,
                            "lastname": customer.last_name,
                            "phone": customer.phone_number,
                            "zip": getattr(customer, 'zipcode', '')
                        }
                        
                        new_contact = client.crm.contacts.basic_api.create(
                            simple_public_object_input_for_create=ContactInput(properties=props)
                        )
                        contact_id = new_contact.id
                        result_entry['status'] = 'created'
                        result_entry['contact_id'] = contact_id
                    
                    success_count += 1
                except Exception as e:
                    result_entry['status'] = 'error'
                    result_entry['error'] = str(e)
                    error_count += 1
            else:
                # Dry run
                result_entry['status'] = 'dry_run'
                success_count += 1
        except Exception as e:
            result_entry['status'] = 'error'
            result_entry['error'] = str(e)
            error_count += 1
        
        results.append(result_entry)
        # Add a small delay to avoid rate limits
        time.sleep(0.5)
    
    return {
        'total': total,
        'success_count': success_count,
        'error_count': error_count,
        'results': results[:50],  # Limit results to avoid huge responses
        'next_start_id': start_id + len(results) if results else start_id
    }
def sync_reservations(start_id=0, limit=None, dry_run=False):
    """Sync reservations to HubSpot"""
    # Add prefetch_related to optimize loading of payments
    query = Reservation.objects.filter(id__gt=start_id).prefetch_related('payments').order_by('id')
    if limit:
        query = query.all()[:limit]
    
    total = query.count()
    results = []
    success_count = 0
    error_count = 0
    
    for reservation in query:
        result_entry = {
            'reservation_id': reservation.id,
            'customer': reservation.customer.get_full_name() if hasattr(reservation, 'customer') else 'Unknown'
        }
        
        try:
            if not dry_run:
                # Log payments for debugging
                payment_data = None
                has_payments = False
                
                if hasattr(reservation, 'payments'):
                    payments = list(reservation.payments.all())
                    has_payments = len(payments) > 0
                    if has_payments:
                        latest_payment = payments[-1]  # Get the last payment
                        payment_data = {
                            'payment_id': latest_payment.id,
                            'status': latest_payment.status,
                            'amount': float(latest_payment.amount) if hasattr(latest_payment, 'amount') and latest_payment.amount else None
                        }
                
                # Log this information
                result_entry['has_payments'] = has_payments
                result_entry['payment_data_before_sync'] = payment_data
                
                # Sync reservation to HubSpot
                result = hubspot_service.sync_reservation_to_hubspot(reservation)
                
                if result.get('success'):
                    result_entry['status'] = 'success'
                    result_entry['deal_id'] = result.get('deal_id')
                    success_count += 1
                    
                    # Check for existing payments and update HubSpot
                    if has_payments and payment_data:
                        try:
                            # Get the latest payment
                            latest_payment = payments[-1]
                            
                            # Map payment status
                            status_map = {
                                "pending": "Pending",
                                "card_saved": "Card On File",
                                "paid": "Paid",
                                "failed": "Failed"
                            }
                            payment_status = status_map.get(latest_payment.status, "Unknown")
                            
                            # Get payment method if available
                            payment_method = "N/A"
                            if hasattr(latest_payment, 'customer') and latest_payment.customer:
                                if hasattr(latest_payment.customer, 'card_brand') and latest_payment.customer.card_brand:
                                    card_brand = latest_payment.customer.card_brand
                                    card_last4 = latest_payment.customer.card_last4
                                    if card_brand and card_last4:
                                        payment_method = f"{card_brand.title()} ending in {card_last4}"
                            
                            # Get payment amount - make sure it's a float
                            payment_amount = None
                            if hasattr(latest_payment, 'amount') and latest_payment.amount is not None:
                                payment_amount = float(latest_payment.amount)
                            else:
                                payment_amount = float(reservation.total_price)
                            
                            # Update the payment info in HubSpot
                            payment_result = hubspot_service.update_deal_payment_status(
                                reservation_id=reservation.id,
                                payment_status=payment_status,
                                payment_amount=payment_amount,
                                payment_method=payment_method
                            )
                            
                            result_entry['payment_synced'] = payment_result.get('success', False)
                            result_entry['payment_status'] = payment_status
                            result_entry['payment_amount'] = payment_amount
                            result_entry['payment_method'] = payment_method
                        except Exception as payment_error:
                            logger.error(f"Error syncing existing payment for reservation #{reservation.id}: {payment_error}")
                            result_entry['payment_error'] = str(payment_error)
                    else:
                        # No payments found - but the deal is still created successfully
                        result_entry['payment_synced'] = False
                        result_entry['payment_status'] = "No payments found"
                else:
                    result_entry['status'] = 'error'
                    result_entry['error'] = result.get('error')
                    error_count += 1
            else:
                # Dry run - just log what would happen
                result_entry['status'] = 'dry_run'
                success_count += 1
        except Exception as e:
            result_entry['status'] = 'error'
            result_entry['error'] = str(e)
            error_count += 1
        
        results.append(result_entry)
        # Add a small delay to avoid rate limits
        time.sleep(0.5)
    
    return {
        'total': total,
        'success_count': success_count,
        'error_count': error_count,
        'results': results[:50],  # Limit results to avoid huge responses
        'next_start_id': start_id + len(results) if results else start_id
    }