"""
GoHighLevel Integration Service

This service handles all interactions with the GoHighLevel Location API,
including contact management, SMS sending, and tag management.
"""

import requests
import re
import logging
from typing import Optional, Dict, Any
from django.conf import settings
from django.utils import timezone
from reservations.models import Lead

logger = logging.getLogger(__name__)


class GoHighLevelService:
    """
    Service class for interacting with GoHighLevel Location API.
    
    This service provides methods to:
    - Create or update contacts in GHL
    - Send SMS messages
    - Find contacts by phone number
    - Manage tags on contacts
    - Map Lead fields to GHL custom fields
    """
    
    base_url = "https://services.leadconnectorhq.com"
    
    def __init__(self):
        """Initialize the service with API credentials from settings."""
        self.api_key = getattr(settings, 'GHL_API_KEY', '')
        self.location_id = getattr(settings, 'GHL_LOCATION_ID', '')
        
        if not self.api_key:
            logger.warning("GHL_API_KEY not set in settings")
        if not self.location_id:
            logger.warning("GHL_LOCATION_ID not set in settings")
        
        # Set headers according to official GHL API v2 documentation
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Version": "2021-07-28"
        }
        
        # Log API key info for debugging (masked for security)
        if self.api_key:
            masked_key = f"{self.api_key[:10]}...{self.api_key[-4:]}" if len(self.api_key) > 14 else "***"
            logger.debug(f"GHL Service initialized - API Key: {masked_key} (length: {len(self.api_key)})")
            logger.debug(f"GHL Service initialized - Location ID: {self.location_id}")
    
    def _get_headers(self) -> Dict[str, str]:
        """Get headers for API requests."""
        return self.headers
    
    def test_api_connection(self) -> Dict[str, Any]:
        """
        Test the API connection and return diagnostic information.
        
        Returns:
            dict: Diagnostic information about the API connection
        """
        diagnostics = {
            "api_key_set": bool(self.api_key),
            "location_id_set": bool(self.location_id),
            "api_key_length": len(self.api_key) if self.api_key else 0,
            "api_key_prefix": self.api_key[:10] + "..." if self.api_key and len(self.api_key) > 10 else self.api_key,
            "connection_test": None,
            "error": None
        }
        
        if not self.api_key or not self.location_id:
            diagnostics["error"] = "API key or Location ID not set"
            return diagnostics
        
        # Try a simple API call to test authentication
        try:
            # Use a simple endpoint to test auth - get locations (if available) or try contacts list
            url = f"{self.base_url}/contacts/"
            params = {"locationId": self.location_id, "limit": 1}
            
            logger.info(f"Testing GHL API connection...")
            logger.debug(f"Testing with URL: {url}")
            logger.debug(f"Testing with headers (masked): Authorization: Bearer {self.api_key[:10]}...")
            
            response = requests.get(url, headers=self.headers, params=params, timeout=10)
            
            diagnostics["connection_test"] = {
                "status_code": response.status_code,
                "success": response.status_code in [200, 201],
            }
            
            if response.status_code == 401:
                diagnostics["error"] = "Invalid JWT - Check your API key"
                diagnostics["error_details"] = response.text
            elif response.status_code in [200, 201]:
                diagnostics["error"] = None
            else:
                diagnostics["error"] = f"Unexpected status: {response.status_code}"
                diagnostics["error_details"] = response.text[:200]
                
        except Exception as e:
            diagnostics["error"] = str(e)
            diagnostics["connection_test"] = {"status_code": None, "success": False}
        
        return diagnostics
    
    @staticmethod
    def _format_phone(phone: str) -> str:
        """
        Format phone number to E.164 format (+1XXXXXXXXXX).
        
        Args:
            phone: Phone number in any format
            
        Returns:
            Formatted phone number in E.164 format
        """
        if not phone:
            return ""
        
        # Remove all non-digit characters
        digits = re.sub(r'\D', '', phone)
        
        # Handle 10-digit numbers (add +1)
        if len(digits) == 10:
            return f"+1{digits}"
        # Handle 11-digit numbers starting with 1 (add +)
        elif len(digits) == 11 and digits[0] == '1':
            return f"+{digits}"
        # If already has +, return as is (assuming it's already E.164)
        elif phone.startswith('+'):
            return phone
        
        # Return original if we can't format it
        return phone
    
    def _map_lead_to_custom_fields(self, lead: Lead) -> Dict[str, Any]:
        """
        Map Lead model fields to GHL custom fields.
        
        Note: Custom field IDs need to be configured in GHL first.
        GHL API v2 expects customFields as an array of objects with 'id' and 'value'.
        
        Args:
            lead: Lead instance
            
        Returns:
            Dictionary with custom field mappings in GHL API v2 format:
            {"customFields": [{"id": "fieldId", "value": "value"}]}
        """
        custom_fields = []
        
        # Pickup Location
        # Field ID: qxfllIiOHmUowHxyiPzo
        if lead.pickup_location:
            custom_fields.append({
                "id": "qxfllIiOHmUowHxyiPzo",
                "value": lead.pickup_location
            })
            logger.debug(f"Mapping pickup_location '{lead.pickup_location}' to custom field")
        
        # Dropoff Location
        # Field ID: 9VIxaKV3LeDEto4VmcYj
        if lead.dropoff_location:
            custom_fields.append({
                "id": "9VIxaKV3LeDEto4VmcYj",
                "value": lead.dropoff_location
            })
            logger.debug(f"Mapping dropoff_location '{lead.dropoff_location}' to custom field")
        
        # Pickup Date
        # Field ID: U9tJFtE5ltig14LWtJnr
        if lead.pickup_date:
            # Format date as YYYY-MM-DD string
            custom_fields.append({
                "id": "U9tJFtE5ltig14LWtJnr",
                "value": lead.pickup_date.strftime('%Y-%m-%d')
            })
            logger.debug(f"Mapping pickup_date '{lead.pickup_date}' to custom field")
        
        # Price (Estimated Price)
        # Field ID: EyM1niSr1adL5PUoQnIv
        if lead.estimated_price:
            # Convert Decimal to string
            custom_fields.append({
                "id": "EyM1niSr1adL5PUoQnIv",
                "value": str(lead.estimated_price)
            })
            logger.debug(f"Mapping estimated_price '{lead.estimated_price}' to custom field")
        
        # Vehicle
        # Field ID: UMD2o916mrxPVHOPBUhO
        if lead.vehicle:
            # Get vehicle type display name (e.g., "Towncar", "SUV", "Mini Van")
            vehicle_name = lead.vehicle.get_vehicle_type_display() if hasattr(lead.vehicle, 'get_vehicle_type_display') else str(lead.vehicle)
            custom_fields.append({
                "id": "UMD2o916mrxPVHOPBUhO",
                "value": vehicle_name
            })
            logger.debug(f"Mapping vehicle '{vehicle_name}' to custom field")
        
        # Trip Type
        # Field ID: rLvcE5ovYliVmNYQnYan
        if lead.trip_type:
            # Format trip type: "oneway" -> "One Way", "roundtrip" -> "Round Trip"
            trip_type_display = lead.get_trip_type_display() if hasattr(lead, 'get_trip_type_display') else lead.trip_type.title()
            custom_fields.append({
                "id": "rLvcE5ovYliVmNYQnYan",
                "value": trip_type_display
            })
            logger.debug(f"Mapping trip_type '{trip_type_display}' to custom field")
        
        # Status
        # Field ID: Sd0n5FsD7X95Y5XtXKkl
        status_display = {
            'new': 'New Lead',
            'contacted': 'Contacted',
            'converted': 'Converted',
            'lost': 'Lost',
            'future_contact': 'Future Contact',
            'interested': 'Interested',
        }.get(lead.status, 'New Lead')
        custom_fields.append({
            "id": "Sd0n5FsD7X95Y5XtXKkl",
            "value": status_display
        })
        logger.debug(f"Mapping status '{lead.status}' to custom field as '{status_display}'")
        
        # Return in GHL API v2 format: { "customFields": [{"id": "fieldId", "value": "value"}] }
        # GHL expects customFields as an array of objects with 'id' and 'value' properties
        return {
            'customFields': custom_fields
        } if custom_fields else {}
    
    def find_contact_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        """
        Find a contact in GHL by phone number.
        
        Args:
            phone: Phone number to search for (will be formatted to E.164)
            
        Returns:
            Contact data if found, None otherwise
        """
        if not self.api_key or not self.location_id:
            logger.error("GHL API credentials not configured")
            return None
        
        formatted_phone = self._format_phone(phone)
        if not formatted_phone:
            logger.warning(f"Invalid phone number: {phone}")
            return None
        
        try:
            # Search for contact by phone using the contacts list endpoint with phone filter
            url = f"{self.base_url}/contacts/"
            
            params = {
                'locationId': self.location_id,
                'phone': formatted_phone,
            }
            
            logger.debug(f"GHL API Request - GET {url} with params: {params}")
            response = requests.get(url, headers=self.headers, params=params, timeout=10)
            
            logger.debug(f"GHL API Response - Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                # GHL API returns contacts in different formats, handle both
                contacts = data.get('contacts', [])
                if not contacts and isinstance(data, list):
                    contacts = data
                if contacts:
                    return contacts[0]  # Return first match
                return None
            elif response.status_code == 404:
                return None
            else:
                error_body = response.text
                try:
                    error_json = response.json()
                    error_body = error_json
                except:
                    pass
                # Don't log as error for 400 - contact might just not exist
                if response.status_code != 400:
                    logger.error(f"Error searching contact: {response.status_code} - Full response: {error_body}")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Exception searching contact by phone: {str(e)}", exc_info=True)
            return None
    
    def create_or_update_contact(self, lead: Lead) -> Optional[str]:
        """
        Create or update a contact in GHL from a Lead.
        
        If contact exists (by phone), updates it. Otherwise creates new.
        Returns the GHL contact ID.
        
        Args:
            lead: Lead instance to sync
            
        Returns:
            GHL contact ID if successful, None otherwise
        """
        if not self.api_key or not self.location_id:
            logger.error("GHL API credentials not configured")
            return None
        
        if not lead.phone:
            logger.warning(f"Lead {lead.id} has no phone number, cannot create GHL contact")
            return None
        
        formatted_phone = self._format_phone(lead.phone)
        if not formatted_phone:
            logger.warning(f"Invalid phone number for lead {lead.id}: {lead.phone}")
            return None
        
        try:
            # Prepare contact data according to GHL API v2 format
            contact_data = {
                "locationId": self.location_id,
                "firstName": lead.first_name or "",
                "lastName": lead.last_name or "",
                "email": lead.email or "",
                "phone": formatted_phone,
            }
            
            # Add custom fields if configured
            custom_fields = self._map_lead_to_custom_fields(lead)
            if custom_fields:
                contact_data.update(custom_fields)
            
            # Debug logging - show exact request being made
            logger.debug(f"GHL API Request - Headers: {self.headers}")
            logger.debug(f"GHL API Request - Contact Data: {contact_data}")
            
            # Try to create contact directly - GHL will return duplicate error if exists
            # This is faster and more reliable than searching first
            url = f"{self.base_url}/contacts/"
            logger.debug(f"GHL API Request - POST {url}")
            
            response = requests.post(url, json=contact_data, headers=self.headers, timeout=10)
            
            logger.debug(f"GHL API Response - Status: {response.status_code}")
            logger.debug(f"GHL API Response - Headers: {dict(response.headers)}")
            logger.debug(f"GHL API Response - Body: {response.text[:500]}")  # First 500 chars
            
            if response.status_code == 200 or response.status_code == 201:
                # Contact created successfully
                try:
                    response_data = response.json()
                    contact = response_data.get('contact', {})
                    contact_id = contact.get('id')
                    if contact_id:
                        logger.info(f"Created GHL contact {contact_id} for lead {lead.id}")
                        return contact_id
                    else:
                        logger.error(f"Contact created but no ID in response: {response_data}")
                        return None
                except ValueError as e:
                    logger.error(f"Failed to parse JSON response: {e} - Response: {response.text}")
                    return None
            elif response.status_code == 400:
                # Handle duplicate contact error - extract contact ID from error response
                try:
                    error_json = response.json()
                    error_message = error_json.get('message', '')
                    
                    # Check if it's a duplicate contact error
                    if 'duplicate' in error_message.lower() or 'duplicated' in error_message.lower():
                        # Extract contact ID from meta field
                        meta = error_json.get('meta', {})
                        contact_id = meta.get('contactId')
                        
                        if contact_id:
                            logger.info(f"Contact already exists in GHL (duplicate): {contact_id} for lead {lead.id}")
                            logger.info(f"Using existing contact ID: {contact_id}")
                            
                            # Try to update the existing contact with latest data
                            # Note: locationId should NOT be included in PUT requests
                            try:
                                update_url = f"{self.base_url}/contacts/{contact_id}"
                                
                                # Prepare update data - exclude locationId for PUT requests
                                update_data = {
                                    "firstName": lead.first_name or "",
                                    "lastName": lead.last_name or "",
                                    "email": lead.email or "",
                                    "phone": formatted_phone,
                                }
                                
                                # Add custom fields if configured (same format as create)
                                if custom_fields:
                                    update_data.update(custom_fields)
                                
                                logger.debug(f"GHL API Request - PUT {update_url} (updating existing contact)")
                                logger.debug(f"GHL API Request - Update Data: {update_data}")
                                update_response = requests.put(update_url, json=update_data, headers=self.headers, timeout=10)
                                
                                logger.debug(f"GHL API Update Response - Status: {update_response.status_code}")
                                if update_response.status_code != 200:
                                    error_body = update_response.text
                                    try:
                                        error_json = update_response.json()
                                        error_body = error_json
                                    except:
                                        pass
                                    logger.warning(f"Could not update contact {contact_id}: {update_response.status_code} - Full response: {error_body}")
                                else:
                                    logger.info(f"Updated existing GHL contact {contact_id} for lead {lead.id}")
                            except Exception as update_error:
                                logger.warning(f"Error updating existing contact: {update_error}", exc_info=True)
                            
                            return contact_id
                        else:
                            logger.warning(f"Duplicate contact error but no contactId in meta: {error_json}")
                            return None
                    else:
                        # Other 400 error
                        logger.error(f"Error creating contact: {response.status_code} - Full response: {error_json}")
                        return None
                except (ValueError, KeyError) as e:
                    logger.error(f"Failed to parse duplicate contact error: {e} - Response: {response.text}")
                    return None
            else:
                error_body = response.text
                try:
                    error_json = response.json()
                    error_body = error_json
                except:
                    pass
                logger.error(f"Error creating contact: {response.status_code} - Full response: {error_body}")
                return None
                    
        except requests.exceptions.RequestException as e:
            logger.error(f"Exception creating/updating contact: {str(e)}", exc_info=True)
            return None
    
    def update_contact_status_fields(self, contact_id: str, status: str, converted: bool = False) -> bool:
        """
        Update only the status custom field in GHL for a contact.
        More efficient than full contact update when only status changes.
        
        Args:
            contact_id: GHL contact ID
            status: Lead status value ('new', 'contacted', 'converted', etc.)
            converted: Whether lead is converted (not used, kept for compatibility)
            
        Returns:
            bool: True if update successful, False otherwise
        """
        if not self.api_key or not self.location_id:
            logger.error("GHL API credentials not configured")
            return False
        
        if not contact_id:
            logger.warning("No contact ID provided for status update")
            return False
        
        try:
            # Map status to display value
            status_display = {
                'new': 'New Lead',
                'contacted': 'Contacted',
                'converted': 'Converted',
                'lost': 'Lost',
                'future_contact': 'Future Contact',
                'interested': 'Interested',
            }.get(status, 'New Lead')
            
            # Prepare update data with only status custom field
            update_data = {
                "customFields": [
                    {
                        "id": "Sd0n5FsD7X95Y5XtXKkl",  # Status field ID
                        "value": status_display
                    }
                ]
            }
            
            update_url = f"{self.base_url}/contacts/{contact_id}"
            
            logger.debug(f"GHL API Request - PUT {update_url} (updating status to '{status_display}')")
            logger.debug(f"GHL API Request - Update Data: {update_data}")
            
            response = requests.put(update_url, json=update_data, headers=self.headers, timeout=10)
            
            logger.debug(f"GHL API Status Update Response - Status: {response.status_code}")
            
            if response.status_code == 200:
                logger.info(f"Successfully updated status to '{status_display}' for GHL contact {contact_id}")
                return True
            else:
                error_body = response.text
                try:
                    error_json = response.json()
                    error_body = error_json
                except:
                    pass
                logger.error(f"Failed to update status for contact {contact_id}: {response.status_code} - {error_body}")
                return False
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Exception updating contact status: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error updating contact status: {str(e)}", exc_info=True)
            return False
    
    def send_sms(self, contact_id: str, message: str) -> bool:
        """
        Send an SMS message to a contact in GHL.
        
        Args:
            contact_id: GHL contact ID
            message: SMS message text
            
        Returns:
            True if successful, False otherwise
        """
        if not self.api_key or not self.location_id:
            logger.error("GHL API credentials not configured")
            return False
        
        if not contact_id or not message:
            logger.warning("Missing contact_id or message for SMS")
            return False
        
        try:
            url = f"{self.base_url}/conversations/messages"
            
            # According to GHL API v2 docs: type should be "SMS" (uppercase)
            payload = {
                "type": "SMS",
                "contactId": contact_id,
                "message": message
            }
            
            logger.debug(f"GHL API Request - POST {url}")
            logger.debug(f"GHL API Request - Payload: {payload}")
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            
            logger.debug(f"GHL API Response - Status: {response.status_code}")
            logger.debug(f"GHL API Response - Body: {response.text[:500]}")
            
            if response.status_code == 200 or response.status_code == 201:
                logger.info(f"Sent SMS to contact {contact_id}")
                return True
            else:
                error_body = response.text
                try:
                    error_json = response.json()
                    error_body = error_json
                except:
                    pass
                logger.error(f"Error sending SMS: {response.status_code} - Full response: {error_body}")
                return False
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Exception sending SMS: {str(e)}", exc_info=True)
            return False
    
    def add_tag(self, contact_id: str, tag: str) -> bool:
        """
        Add a tag to a contact in GHL.
        
        Args:
            contact_id: GHL contact ID
            tag: Tag name to add
            
        Returns:
            True if successful, False otherwise
        """
        if not self.api_key or not self.location_id:
            logger.error("GHL API credentials not configured")
            return False
        
        if not contact_id or not tag:
            logger.warning("Missing contact_id or tag")
            return False
        
        try:
            # First, get current contact to see existing tags
            url = f"{self.base_url}/contacts/{contact_id}"
            headers = self._get_headers()
            
            # Get current contact
            response = requests.get(url, headers=headers, params={'locationId': self.location_id}, timeout=10)
            
            logger.debug(f"GHL API Response - Status: {response.status_code}")
            
            if response.status_code != 200:
                error_body = response.text
                try:
                    error_json = response.json()
                    error_body = error_json
                except:
                    pass
                logger.error(f"Error fetching contact for tag: {response.status_code} - Full response: {error_body}")
                return False
            
            contact = response.json().get('contact', {})
            current_tags = contact.get('tags', [])
            
            # Add new tag if not already present
            if tag not in current_tags:
                current_tags.append(tag)
                
                # Update contact with new tags
                update_data = {
                    'locationId': self.location_id,
                    'tags': current_tags,
                }
                
                logger.debug(f"GHL API Request - PUT {url} with update_data: {update_data}")
                update_response = requests.put(url, json=update_data, headers=self.headers, timeout=10)
                
                logger.debug(f"GHL API Response - Status: {update_response.status_code}")
                
                if update_response.status_code == 200:
                    logger.info(f"Added tag '{tag}' to contact {contact_id}")
                    return True
                else:
                    error_body = update_response.text
                    try:
                        error_json = update_response.json()
                        error_body = error_json
                    except:
                        pass
                    logger.error(f"Error adding tag: {update_response.status_code} - Full response: {error_body}")
                    return False
            else:
                logger.info(f"Tag '{tag}' already exists on contact {contact_id}")
                return True
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Exception adding tag: {str(e)}")
            return False
    
    def remove_tag(self, contact_id: str, tag: str) -> bool:
        """
        Remove a tag from a contact in GHL.
        
        Args:
            contact_id: GHL contact ID
            tag: Tag name to remove
            
        Returns:
            True if successful, False otherwise
        """
        if not self.api_key or not self.location_id:
            logger.error("GHL API credentials not configured")
            return False
        
        if not contact_id or not tag:
            logger.warning("Missing contact_id or tag")
            return False
        
        try:
            # First, get current contact to see existing tags
            url = f"{self.base_url}/contacts/{contact_id}"
            
            # Get current contact
            logger.debug(f"GHL API Request - GET {url} (for tag removal)")
            response = requests.get(url, headers=self.headers, params={'locationId': self.location_id}, timeout=10)
            
            logger.debug(f"GHL API Response - Status: {response.status_code}")
            
            if response.status_code != 200:
                error_body = response.text
                try:
                    error_json = response.json()
                    error_body = error_json
                except:
                    pass
                logger.error(f"Error fetching contact for tag removal: {response.status_code} - Full response: {error_body}")
                return False
            
            contact = response.json().get('contact', {})
            current_tags = contact.get('tags', [])
            
            # Remove tag if present
            if tag in current_tags:
                current_tags.remove(tag)
                
                # Update contact with updated tags
                update_data = {
                    'locationId': self.location_id,
                    'tags': current_tags,
                }
                
                logger.debug(f"GHL API Request - PUT {url} with update_data: {update_data}")
                update_response = requests.put(url, json=update_data, headers=self.headers, timeout=10)
                
                logger.debug(f"GHL API Response - Status: {update_response.status_code}")
                
                if update_response.status_code == 200:
                    logger.info(f"Removed tag '{tag}' from contact {contact_id}")
                    return True
                else:
                    error_body = update_response.text
                    try:
                        error_json = update_response.json()
                        error_body = error_json
                    except:
                        pass
                    logger.error(f"Error removing tag: {update_response.status_code} - Full response: {error_body}")
                    return False
            else:
                logger.info(f"Tag '{tag}' not found on contact {contact_id}")
                return True
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Exception removing tag: {str(e)}")
            return False


def get_sms_template(lead: Lead) -> str:
    """
    Generate the SMS message template for a lead.
    
    Args:
        lead: Lead instance
        
    Returns:
        Formatted SMS message string
    """
    # Format pickup date nicely
    pickup_date_str = "your upcoming trip"
    if lead.pickup_date:
        pickup_date_str = lead.pickup_date.strftime("%B %d")
    
    # Build message
    message = (
        f"Hey {lead.first_name or 'there'}, this is Grayson Towncar. "
        f"Do you still need transportation from {lead.pickup_location or 'your pickup location'} "
        f"to {lead.dropoff_location or 'your destination'} on {pickup_date_str}? "
    )
    
    return message
