# users/hubspot_admin_dashboard.py

import threading
import uuid
from django.shortcuts import render, redirect
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from reservations.models import Reservation
import reservations.hubspot_service as hubspot_service

# Global tracking variables
active_sync = None
cancel_requested = False
current_item = ""
processed_count = 0
total_count = 0

@staff_member_required
def hubspot_dashboard(request):
    """Admin dashboard for HubSpot synchronization"""
    global active_sync, cancel_requested, current_item, processed_count, total_count
    context = {}
    
    # Check if a sync is currently running
    context['active_sync'] = active_sync
    
    if request.method == 'POST':
        if 'confirm_sync' in request.POST and not active_sync:
            # Extract sync parameters
            sync_type = request.POST.get('type', 'reservations')
            limit = request.POST.get('limit', '')
            start_id = request.POST.get('start_id', '0')
            dry_run = 'dry_run' in request.POST
            
            # Convert to proper types
            if limit and limit.strip() and limit.lower() != 'none':
                try:
                    limit = int(limit)
                except (ValueError, TypeError):
                    limit = None
            else:
                limit = None
                
            if start_id and start_id.strip() and start_id.lower() != 'none':
                try:
                    start_id = int(start_id)
                except (ValueError, TypeError):
                    start_id = 0
            else:
                start_id = 0
            
            # Store parameters in context for form repopulation
            context['sync_type'] = sync_type
            context['limit'] = limit
            context['start_id'] = start_id
            context['dry_run'] = dry_run
            
            # Reset tracking variables
            current_item = "Initializing..."
            processed_count = 0
            total_count = 0
            cancel_requested = False
            
            # Set active sync info
            active_sync = {
                'sync_type': sync_type,
                'limit': limit,
                'start_id': start_id,
                'dry_run': dry_run,
                'in_progress': True
            }
            
            # Perform the sync
            results = {}
            
            try:
                if sync_type in ['reservations', 'all']:
                    results['reservations'] = sync_reservations_with_cancel(start_id, limit, dry_run)
                    
                if not cancel_requested and sync_type in ['customers', 'all']:
                    results['customers'] = sync_customers_with_cancel(start_id, limit, dry_run)
            except Exception as e:
                import traceback
                error_details = traceback.format_exc()
                context['error'] = str(e)
                context['error_details'] = error_details
            finally:
                # Always clear active sync when done
                active_sync = None
                
            context['results'] = results
            
        elif 'cancel_sync' in request.POST and active_sync:
            # Mark as cancelled
            cancel_requested = True
            context['cancel_requested'] = True
    
    return render(request, 'admin/hubspot_dashboard.html', context)

@staff_member_required
def hubspot_status(request):
    """AJAX endpoint to get the current sync status"""
    global active_sync, current_item, processed_count, total_count
    
    if active_sync:
        progress = int((processed_count / total_count * 100) if total_count > 0 else 0)
        return JsonResponse({
            'active': True,
            'progress': progress,
            'processed': processed_count,
            'total': total_count,
            'current_item': current_item
        })
    else:
        return JsonResponse({
            'active': False
        })

def sync_reservations_with_cancel(start_id=0, limit=None, dry_run=False):
    """Modified sync function that checks for cancellation requests"""
    global cancel_requested, current_item, processed_count, total_count
    
    query = Reservation.objects.filter(id__gt=start_id).order_by('id')
    if limit:
        query = query.all()[:limit]
    
    total = query.count()
    total_count = total  # Update global tracking variable
    processed_count = 0  # Reset counter
    
    results = []
    success_count = 0
    error_count = 0
    processed = 0
    
    for reservation in query:
        # Update current item for status tracking
        current_item = f"Reservation #{reservation.id} - {reservation.customer.get_full_name() if hasattr(reservation, 'customer') else 'Unknown'}"
        
        # Check for cancellation
        if cancel_requested:
            break
            
        processed += 1
        processed_count = processed  # Update tracking variable
        
        # Prepare result entry
        result_entry = {
            'reservation_id': reservation.id,
            'customer': reservation.customer.get_full_name() if hasattr(reservation, 'customer') else 'Unknown'
        }
        
        try:
            if not dry_run:
                # DIRECT CALL to hubspot_service functions
                # Check if deal already exists
                existing_deal_id = hubspot_service.find_deal_by_reservation_id(reservation.id)
                
                if existing_deal_id:
                    # Deal already exists
                    result_entry['status'] = 'success'
                    result_entry['deal_id'] = existing_deal_id
                    success_count += 1
                else:
                    # Create contact and deal
                    contact_id = hubspot_service.create_or_find_contact(reservation)
                    if contact_id:
                        deal_id = hubspot_service.create_deal(reservation, contact_id)
                        if deal_id:
                            result_entry['status'] = 'success'
                            result_entry['deal_id'] = deal_id
                            success_count += 1
                        else:
                            result_entry['status'] = 'error'
                            result_entry['error'] = "Failed to create deal"
                            error_count += 1
                    else:
                        result_entry['status'] = 'error'
                        result_entry['error'] = "Failed to create or find contact"
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
    
    return {
        'total': total,
        'success_count': success_count,
        'error_count': error_count,
        'results': results[:50],  # Limit results to avoid huge responses
        'next_start_id': start_id + processed if processed > 0 else start_id,
        'cancelled': cancel_requested
    }

def sync_customers_with_cancel(start_id=0, limit=None, dry_run=False):
    """Sync customers to HubSpot with cancellation support"""
    global cancel_requested, current_item, processed_count, total_count
    
    # Import here to avoid circular imports
    from reservations.models import Customer
    import os
    from hubspot import HubSpot
    from hubspot.crm.contacts import SimplePublicObjectInput as ContactInput
    
    query = Customer.objects.filter(id__gt=start_id).order_by('id')
    if limit:
        query = query.all()[:limit]
    
    total = query.count()
    # Update global total count (add to existing if we already processed reservations)
    total_count = total_count + total if total_count > 0 else total
    # Reset processed counter if we're starting fresh with customers
    if processed_count == 0 or processed_count >= total_count - total:
        processed_count = 0
    
    results = []
    success_count = 0
    error_count = 0
    processed = 0
    
    HUBSPOT_TOKEN = os.environ.get('HUBSPOT_TOKEN')
    if not HUBSPOT_TOKEN:
        return {
            'success': False,
            'error': 'HUBSPOT_TOKEN not found',
            'total': total,
            'success_count': 0,
            'error_count': total,
            'cancelled': cancel_requested
        }
    
    client = HubSpot(access_token=HUBSPOT_TOKEN)
    
    for customer in query:
        # Update current item for status
        current_item = f"Customer #{customer.id} - {customer.get_full_name()}"
        
        # Check for cancellation
        if cancel_requested:
            break
            
        processed += 1
        processed_count += 1  # Update global counter
        
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
    
    return {
        'total': total,
        'success_count': success_count,
        'error_count': error_count,
        'results': results[:50],
        'next_start_id': start_id + processed if processed > 0 else start_id,
        'cancelled': cancel_requested
    }