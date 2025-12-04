"""
Simple AeroAPI service for flight tracking.
Uses the /flights/{ident} endpoint to get flight information.
"""
import requests
import logging
from django.conf import settings
from django.utils import timezone as django_timezone
from typing import Dict, Optional, Any
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class AeroAPIService:
    """
    Simple service to fetch flight data from AeroAPI using /flights/{ident} endpoint
    """
    
    def __init__(self):
        self.api_key = getattr(settings, 'AEROAPI_KEY', None)
        self.base_url = getattr(settings, 'AEROAPI_BASE_URL', 'https://aeroapi.flightaware.com/aeroapi')
        
    def get_flight_info(self, flight_ident: str, flight_date: Optional[str] = None, trip_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Get flight information using AeroAPI /flights/{ident} endpoint
        
        Args:
            flight_ident: Flight identifier in IATA format (e.g., 'DL1691')
            flight_date: Optional date in YYYY-MM-DD format to filter flights for a specific date
            trip_type: Optional trip type ('arrival', 'return', 'other') to determine which times to parse
            
        Returns:
            Dict containing flight information or error details
        """
        if not self.api_key:
            return {
                'error': 'AeroAPI key not configured',
                'status': 'error'
            }
        
        if not flight_ident:
            return {
                'error': 'Flight identifier is required',
                'status': 'error'
            }
        
        # Clean flight identifier
        flight_ident = flight_ident.strip().upper()
        
        try:
            url = f"{self.base_url}/flights/{flight_ident}"
            headers = {
                'x-apikey': self.api_key
            }
            
            # Note: /flights/{ident} endpoint doesn't accept date parameter
            # It returns a list of flights, we'll filter by date in the response
            if flight_date:
                logger.info(f"Fetching flight info for {flight_ident} (will filter for date {flight_date}) from AeroAPI")
            else:
                logger.info(f"Fetching flight info for {flight_ident} from AeroAPI")
            
            response = requests.get(url, headers=headers, timeout=10)
            
            logger.info(f"AeroAPI Response Status: {response.status_code}")
            
            # Handle rate limiting (429 Too Many Requests)
            if response.status_code == 429:
                retry_after = response.headers.get('Retry-After', '60')  # Default to 60 seconds
                try:
                    retry_after = int(retry_after)
                except ValueError:
                    retry_after = 60
                
                logger.warning(f"AeroAPI rate limit exceeded. Retry after {retry_after} seconds")
                return {
                    'error': f'Rate limit exceeded. Please wait {retry_after} seconds before retrying.',
                    'status': 'rate_limited',
                    'retry_after': retry_after,
                    'flight_ident': flight_ident
                }
            
            if response.status_code == 404:
                return {
                    'error': f'Flight {flight_ident} not found',
                    'status': 'not_found',
                    'flight_ident': flight_ident
                }
            
            response.raise_for_status()
            data = response.json()
            
            logger.info(f"AeroAPI Response Data keys: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
            logger.info(f"AeroAPI Response Data (first 1000 chars): {str(data)[:1000]}")
            
            # AeroAPI returns flights in a 'flights' array, get the first flight that involves MCO
            if isinstance(data, dict) and 'flights' in data:
                flights = data.get('flights', [])
                if not flights:
                    return {
                        'error': 'No flights found in response',
                        'status': 'not_found'
                    }
                
                # Filter to find flights involving MCO (Orlando International Airport) and matching date if provided
                target_date = None
                if flight_date:
                    try:
                        from datetime import datetime as dt
                        target_date = dt.strptime(flight_date, '%Y-%m-%d').date()
                    except ValueError:
                        logger.warning(f"Invalid date format: {flight_date}, ignoring date filter")
                
                candidates = []
                
                for flight in flights:
                    origin = flight.get('origin', {})
                    destination = flight.get('destination', {})
                    
                    if not isinstance(origin, dict) or not isinstance(destination, dict):
                        continue
                    
                    # Must be arriving at MCO OR departing from MCO
                    origin_code = origin.get('code_iata', '')
                    dest_code = destination.get('code_iata', '')
                    
                    if dest_code != 'MCO' and origin_code != 'MCO':
                        continue
                    
                    # Determine if this is an arrival or departure
                    is_arrival = dest_code == 'MCO'
                    is_departure = origin_code == 'MCO'
                    
                    # If we have a target date, check if this flight matches
                    if target_date:
                        # For arrivals, check scheduled_on (arrival time)
                        # For departures, check scheduled_off (departure time)
                        scheduled_time = None
                        
                        if is_arrival:
                            scheduled_time = flight.get('scheduled_on')
                        elif is_departure:
                            scheduled_time = flight.get('scheduled_off')
                        
                        if not scheduled_time:
                            continue
                        
                        # scheduled_time can be a dict with date/localtime/epoch, or an ISO string
                        scheduled_dt = None
                        
                        if isinstance(scheduled_time, dict):
                            # Try localtime first (already in airport timezone)
                            if scheduled_time.get('localtime'):
                                scheduled_dt = self._parse_datetime(scheduled_time.get('localtime'))
                                if scheduled_dt and not scheduled_dt.tzinfo:
                                    scheduled_dt = scheduled_dt.replace(tzinfo=ZoneInfo('America/New_York'))
                            elif scheduled_time.get('date'):
                                # date field might be ISO string
                                date_str = scheduled_time.get('date')
                                try:
                                    # Try parsing as ISO string
                                    if 'T' in date_str or 'Z' in date_str:
                                        scheduled_dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                                    else:
                                        scheduled_dt = self._parse_datetime(date_str)
                                except Exception:
                                    scheduled_dt = self._parse_datetime(date_str)
                            elif scheduled_time.get('epoch'):
                                try:
                                    scheduled_dt = datetime.fromtimestamp(scheduled_time.get('epoch'), tz=ZoneInfo('UTC'))
                                except (ValueError, TypeError, OSError):
                                    continue
                        elif isinstance(scheduled_time, str):
                            # ISO string like "2025-12-03T22:13:00Z"
                            try:
                                scheduled_dt = datetime.fromisoformat(scheduled_time.replace('Z', '+00:00'))
                            except ValueError:
                                scheduled_dt = self._parse_datetime(scheduled_time)
                        
                        if scheduled_dt:
                            # Convert to Eastern Time and check date
                            if scheduled_dt.tzinfo:
                                scheduled_dt = scheduled_dt.astimezone(ZoneInfo('America/New_York'))
                            else:
                                scheduled_dt = scheduled_dt.replace(tzinfo=ZoneInfo('America/New_York'))
                            
                            if scheduled_dt.date() == target_date:
                                flight_type = "arriving" if is_arrival else "departing"
                                candidates.append((scheduled_dt, flight))
                                logger.info(f"Found MCO flight candidate for {target_date}: {flight.get('ident_iata', 'Unknown')} {flight_type} {scheduled_dt}")
                    else:
                        # No date filter, just collect MCO flights
                        candidates.append((None, flight))
                
                # Pick the best match
                if candidates:
                    if target_date and len(candidates) > 1:
                        # Sort by closest to target date/time, pick the first one
                        candidates.sort(key=lambda x: x[0] if x[0] else datetime.min.replace(tzinfo=ZoneInfo('America/New_York')))
                    flight_data = candidates[0][1]
                    logger.info(f"Selected flight: {flight_data.get('ident_iata', 'Unknown')}")
                else:
                    # If no MCO flight found for the date, use first MCO flight as fallback
                    mco_fallback = None
                    for flight in flights:
                        origin = flight.get('origin', {})
                        destination = flight.get('destination', {})
                        origin_code = origin.get('code_iata', '') if isinstance(origin, dict) else ''
                        dest_code = destination.get('code_iata', '') if isinstance(destination, dict) else ''
                        
                        if origin_code == 'MCO' or dest_code == 'MCO':
                            mco_fallback = flight
                            break
                    
                    if mco_fallback:
                        flight_data = mco_fallback
                        logger.warning(f"No MCO flight found for date {target_date}, using first MCO flight: {flight_data.get('ident_iata', 'Unknown')}")
                    else:
                        # Last resort: use first flight
                        flight_data = flights[0]
                        logger.warning(f"No MCO flight found, using first flight: {flight_data.get('ident_iata', 'Unknown')}")
            elif isinstance(data, dict):
                # Single flight object (shouldn't happen but handle it)
                # Verify it's an MCO flight
                origin = data.get('origin', {})
                destination = data.get('destination', {})
                origin_code = origin.get('code_iata', '') if isinstance(origin, dict) else ''
                dest_code = destination.get('code_iata', '') if isinstance(destination, dict) else ''
                
                if origin_code != 'MCO' and dest_code != 'MCO':
                    return {
                        'error': f'Flight does not involve MCO (Origin: {origin_code}, Destination: {dest_code})',
                        'status': 'not_mco'
                    }
                
                flight_data = data
            elif isinstance(data, list):
                # Direct array of flights
                if not data:
                    return {
                        'error': 'No flights found in response',
                        'status': 'not_found'
                    }
                
                # Filter to find MCO flight
                mco_flight = None
                for flight in data:
                    origin = flight.get('origin', {})
                    destination = flight.get('destination', {})
                    origin_code = origin.get('code_iata', '') if isinstance(origin, dict) else ''
                    dest_code = destination.get('code_iata', '') if isinstance(destination, dict) else ''
                    
                    if origin_code == 'MCO' or dest_code == 'MCO':
                        mco_flight = flight
                        logger.info(f"Found MCO flight: {flight.get('ident_iata', 'Unknown')} ({origin_code} -> {dest_code})")
                        break
                
                if not mco_flight:
                    mco_flight = data[0]
                    logger.warning(f"No MCO flight found, using first flight: {mco_flight.get('ident_iata', 'Unknown')}")
                
                flight_data = mco_flight
            else:
                return {
                    'error': 'Unexpected response format',
                    'status': 'error'
                }
            
            # Parse the flight data
            try:
                parsed_data = self._parse_flight_data(flight_data, trip_type=trip_type)
                logger.info(f"Parsed Flight Data: {parsed_data}")
                return parsed_data
            except Exception as parse_error:
                logger.error(f"Error parsing AeroAPI response: {parse_error}", exc_info=True)
                return {
                    'error': f'Error parsing flight data: {str(parse_error)}',
                    'status': 'error',
                    'raw_data': str(flight_data)[:500]  # Include first 500 chars for debugging
                }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"AeroAPI request failed: {e}")
            return {
                'error': f'API request failed: {str(e)}',
                'status': 'error'
            }
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return {
                'error': f'Unexpected error: {str(e)}',
                'status': 'error'
            }
    
    def _parse_flight_data(self, data: Dict, trip_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Parse AeroAPI response and extract relevant fields
        
        Args:
            data: Raw JSON response from AeroAPI (single flight object)
            trip_type: Optional trip type ('arrival', 'return', 'other') to determine which times to parse
                      - 'arrival': Parse arrival times at MCO (scheduled_on/estimated_on)
                      - 'return': Parse departure times from MCO (scheduled_off/estimated_off) and destination arrival times
            
        Returns:
            Parsed flight data dictionary
        """
        try:
            # AeroAPI returns flight data with various fields
            # Extract the most relevant information
            origin_data = data.get('origin', {}) or {}
            destination_data = data.get('destination', {}) or {}
            
            # Helper function to parse and convert datetime fields to Eastern Time
            def parse_and_convert_to_eastern(time_field):
                """Parse a time field and convert to America/New_York timezone"""
                if not time_field:
                    return None
                
                parsed_dt = None
                
                if isinstance(time_field, dict):
                    # Prefer localtime (already in airport's local timezone - MCO is America/New_York)
                    if time_field.get('localtime'):
                        localtime_str = time_field.get('localtime')
                        parsed_dt = self._parse_datetime(localtime_str)
                        # If parsed successfully and it's naive, assume it's Eastern Time
                        if parsed_dt and not parsed_dt.tzinfo:
                            parsed_dt = parsed_dt.replace(tzinfo=ZoneInfo('America/New_York'))
                    elif time_field.get('date'):
                        parsed_dt = self._parse_datetime(time_field.get('date'))
                        # If naive, assume UTC and convert to Eastern
                        if parsed_dt and not parsed_dt.tzinfo:
                            parsed_dt = parsed_dt.replace(tzinfo=ZoneInfo('UTC'))
                    elif time_field.get('epoch'):
                        # Convert epoch to datetime (epoch is always UTC)
                        try:
                            parsed_dt = datetime.fromtimestamp(time_field.get('epoch'), tz=ZoneInfo('UTC'))
                        except (ValueError, TypeError, OSError):
                            return None
                elif isinstance(time_field, str):
                    parsed_dt = self._parse_datetime(time_field)
                    # If naive, assume it's Eastern Time (from localtime field)
                    if parsed_dt and not parsed_dt.tzinfo:
                        parsed_dt = parsed_dt.replace(tzinfo=ZoneInfo('America/New_York'))
                
                # Convert to Eastern Time if timezone-aware
                if parsed_dt and parsed_dt.tzinfo:
                    eastern_tz = ZoneInfo('America/New_York')
                    parsed_dt = parsed_dt.astimezone(eastern_tz)
                elif parsed_dt:
                    # If naive, assume it's already in Eastern Time and make it timezone-aware
                    eastern_tz = ZoneInfo('America/New_York')
                    parsed_dt = parsed_dt.replace(tzinfo=eastern_tz)
                
                return parsed_dt
            
            # Determine if this is an arrival at MCO or departure from MCO
            origin_code = origin_data.get('code_iata', '') if isinstance(origin_data, dict) else ''
            dest_code = destination_data.get('code_iata', '') if isinstance(destination_data, dict) else ''
            is_arrival_at_mco = dest_code == 'MCO'
            is_departure_from_mco = origin_code == 'MCO'
            
            # Parse times based on trip type and flight direction
            scheduled_runway_arrival = None
            estimated_runway_arrival = None
            actual_runway_arrival = None
            scheduled_gate_arrival = None
            estimated_gate_arrival = None
            actual_gate_arrival = None
            scheduled_dest_arrival = None
            estimated_dest_arrival = None
            actual_dest_arrival = None
            scheduled_dest_gate_arrival = None
            estimated_dest_gate_arrival = None
            actual_dest_gate_arrival = None
            
            # For arrival trips (arriving at MCO): use scheduled_on/estimated_on (arrival at MCO)
            # For return trips (departing from MCO): use scheduled_off/estimated_off (departure from MCO)
            if trip_type == 'arrival' or (trip_type != 'return' and is_arrival_at_mco):
                # Arrival at MCO - use arrival times
                scheduled_runway_arrival = parse_and_convert_to_eastern(data.get('scheduled_on'))
                estimated_runway_arrival = parse_and_convert_to_eastern(data.get('estimated_on'))
                actual_runway_arrival = parse_and_convert_to_eastern(data.get('actual_on'))
                
                scheduled_gate_arrival = parse_and_convert_to_eastern(data.get('scheduled_in'))
                estimated_gate_arrival = parse_and_convert_to_eastern(data.get('estimated_in'))
                actual_gate_arrival = parse_and_convert_to_eastern(data.get('actual_in'))
            elif trip_type == 'return' or is_departure_from_mco:
                # Departure from MCO - use departure times (scheduled_off/estimated_off)
                scheduled_runway_arrival = parse_and_convert_to_eastern(data.get('scheduled_off'))
                estimated_runway_arrival = parse_and_convert_to_eastern(data.get('estimated_off'))
                actual_runway_arrival = parse_and_convert_to_eastern(data.get('actual_off'))
                
                scheduled_gate_arrival = parse_and_convert_to_eastern(data.get('scheduled_out'))
                estimated_gate_arrival = parse_and_convert_to_eastern(data.get('estimated_out'))
                actual_gate_arrival = parse_and_convert_to_eastern(data.get('actual_out'))
                
                # Also parse destination arrival times for return trips (when plane lands at destination)
                # These can be useful to show when the passenger's flight arrives at their destination
                scheduled_dest_arrival = parse_and_convert_to_eastern(data.get('scheduled_on'))
                estimated_dest_arrival = parse_and_convert_to_eastern(data.get('estimated_on'))
                actual_dest_arrival = parse_and_convert_to_eastern(data.get('actual_on'))
                
                scheduled_dest_gate_arrival = parse_and_convert_to_eastern(data.get('scheduled_in'))
                estimated_dest_gate_arrival = parse_and_convert_to_eastern(data.get('estimated_in'))
                actual_dest_gate_arrival = parse_and_convert_to_eastern(data.get('actual_in'))
            else:
                # Default: try arrival times first, fallback to departure times
                scheduled_runway_arrival = parse_and_convert_to_eastern(data.get('scheduled_on')) or parse_and_convert_to_eastern(data.get('scheduled_off'))
                estimated_runway_arrival = parse_and_convert_to_eastern(data.get('estimated_on')) or parse_and_convert_to_eastern(data.get('estimated_off'))
                actual_runway_arrival = parse_and_convert_to_eastern(data.get('actual_on')) or parse_and_convert_to_eastern(data.get('actual_off'))
                
                scheduled_gate_arrival = parse_and_convert_to_eastern(data.get('scheduled_in')) or parse_and_convert_to_eastern(data.get('scheduled_out'))
                estimated_gate_arrival = parse_and_convert_to_eastern(data.get('estimated_in')) or parse_and_convert_to_eastern(data.get('estimated_out'))
                actual_gate_arrival = parse_and_convert_to_eastern(data.get('actual_in')) or parse_and_convert_to_eastern(data.get('actual_out'))
            
            # Keep backward compatibility: use runway times for the old fields
            scheduled_arrival = scheduled_runway_arrival
            estimated_arrival = estimated_runway_arrival
            
            # Get terminal, gate, baggage - for arrivals use destination, for departures use origin
            terminal = ''
            gate = ''
            baggage_claim = ''
            
            if trip_type == 'return' or is_departure_from_mco:
                # For departures, get gate/terminal from origin (MCO)
                if isinstance(origin_data, dict):
                    terminal = origin_data.get('terminal', '')
                    gate = origin_data.get('gate', '')
            else:
                # For arrivals, get gate/terminal from destination (MCO)
                if isinstance(destination_data, dict):
                    terminal = destination_data.get('terminal', '')
                    gate = destination_data.get('gate', '')
                    baggage_claim = destination_data.get('baggage', '')
            
            # Get flight status - AeroAPI uses various status fields
            status = data.get('status', '') or ''
            if not status:
                # Try alternative status fields
                if data.get('cancelled'):
                    status = 'Cancelled'
                elif data.get('diverted'):
                    status = 'Diverted'
                elif data.get('position_only'):
                    status = 'Position Only'
                else:
                    # Try to determine from progress
                    progress = data.get('progress_percent')
                    if progress is not None:
                        if progress == 0:
                            status = 'Scheduled'
                        elif progress < 100:
                            status = 'En Route'
                        else:
                            status = 'Landed'
            
            result = {
                'status': 'success',  # This indicates parsing was successful
                'flight_iata': data.get('ident_iata', '') or data.get('ident', ''),
                'origin': self._format_airport(origin_data),
                'destination': self._format_airport(destination_data),
                'flight_status': status,  # This is the actual flight status (Scheduled, En Route, etc.)
                
                # MCO times (arrival at MCO or departure from MCO, depending on trip type)
                'scheduled_runway_arrival_local': scheduled_runway_arrival,
                'estimated_runway_arrival_local': estimated_runway_arrival,
                'actual_runway_arrival_local': actual_runway_arrival,
                
                'scheduled_gate_arrival_local': scheduled_gate_arrival,
                'estimated_gate_arrival_local': estimated_gate_arrival,
                'actual_gate_arrival_local': actual_gate_arrival,
                
                # Backward compatibility: keep old field names
                'scheduled_arrival_local': scheduled_arrival,
                'estimated_arrival_local': estimated_arrival,
                
                # Status details
                'cancelled': data.get('cancelled', False),
                'diverted': data.get('diverted', False),
                'progress_percent': data.get('progress_percent'),
                
                'terminal': terminal,
                'gate': gate,
                'baggage_claim': baggage_claim,
                'last_updated': django_timezone.now()
            }
            
            # Add destination arrival times for return trips (when plane lands at destination airport)
            if trip_type == 'return' or is_departure_from_mco:
                result['scheduled_dest_arrival_local'] = scheduled_dest_arrival
                result['estimated_dest_arrival_local'] = estimated_dest_arrival
                result['actual_dest_arrival_local'] = actual_dest_arrival
                result['scheduled_dest_gate_arrival_local'] = scheduled_dest_gate_arrival
                result['estimated_dest_gate_arrival_local'] = estimated_dest_gate_arrival
                result['actual_dest_gate_arrival_local'] = actual_dest_gate_arrival
            
            return result
            
        except Exception as e:
            logger.error(f"Error parsing AeroAPI flight data: {e}")
            return {
                'error': f'Error parsing flight data: {str(e)}',
                'status': 'error'
            }
    
    def _format_airport(self, airport_data: Dict) -> str:
        """
        Format airport data into readable string (e.g., "SLC - Salt Lake City Intl")
        
        Args:
            airport_data: Airport dictionary from AeroAPI
            
        Returns:
            Formatted airport string
        """
        if not airport_data:
            return ''
        
        code = airport_data.get('code_iata', airport_data.get('code_icao', ''))
        name = airport_data.get('name', '')
        
        if code and name:
            return f"{code} - {name}"
        elif code:
            return code
        elif name:
            return name
        return ''
    
    def _parse_datetime(self, datetime_str: Optional[str]) -> Optional[datetime]:
        """
        Parse datetime string from AeroAPI response
        
        Args:
            datetime_str: Datetime string from API (e.g., "2025-12-03 17:23:00")
            
        Returns:
            Parsed datetime object or None
        """
        if not datetime_str:
            return None
        
        try:
            # AeroAPI localtime format is typically: "2025-12-03 17:23:00" or ISO format
            # Try common formats including ISO 8601 with timezone
            formats = [
                '%Y-%m-%d %H:%M:%S',      # Most common AeroAPI format: "2025-12-03 17:23:00"
                '%Y-%m-%dT%H:%M:%S',     # ISO without timezone
                '%Y-%m-%dT%H:%M:%S%z',   # ISO with timezone
                '%Y-%m-%d %H:%M:%S%z',   # Space separated with timezone
                '%Y-%m-%dT%H:%M:%S.%f',  # ISO with microseconds
                '%Y-%m-%dT%H:%M:%S.%f%z', # ISO with microseconds and timezone
                '%Y-%m-%d %H:%M:%S.%f',   # Space separated with microseconds
                '%Y-%m-%d %H:%M:%S.%f%z', # Space separated with microseconds and timezone
            ]
            
            for fmt in formats:
                try:
                    return datetime.strptime(datetime_str, fmt)
                except ValueError:
                    continue
            
            # Try parsing with dateutil if available (more flexible)
            try:
                from dateutil import parser
                return parser.parse(datetime_str)
            except (ImportError, ValueError, TypeError):
                pass
            
            logger.warning(f"Could not parse datetime: {datetime_str}")
            return None
            
        except Exception as e:
            logger.error(f"Error parsing datetime {datetime_str}: {e}")
            return None

