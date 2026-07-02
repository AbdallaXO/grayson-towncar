"""
AeroAPI service for flight tracking.
Uses /flights/{ident} for live data (within ~48hrs) and
/schedules/{start}/{end} for scheduled data (up to 1 year out).
"""
import re
import requests
import logging
from django.conf import settings
from django.utils import timezone as django_timezone
from typing import Dict, Optional, Any, Tuple
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Airports we serve ground transportation for. Used to identify which flight
# in an AeroAPI response is the customer's relevant leg.
#   MCO = Orlando International (major hub)
#   SFB = Orlando Sanford (Allegiant, Avelo, Breeze, Sun Country)
#   LAL = Lakeland Linder (occasional)
ORLANDO_AIRPORT_CODES = ("MCO", "SFB", "LAL")


class AeroAPIService:
    """
    Simple service to fetch flight data from AeroAPI using /flights/{ident} endpoint
    """
    
    # Hours threshold: use /schedules/ for flights beyond this, /flights/ within
    SCHEDULE_THRESHOLD_HOURS = 48

    def __init__(self):
        self.api_key = getattr(settings, 'AEROAPI_KEY', None)
        self.base_url = getattr(settings, 'AEROAPI_BASE_URL', 'https://aeroapi.flightaware.com/aeroapi')
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({'x-apikey': self.api_key})

    @staticmethod
    def _split_flight_ident(flight_ident: str) -> Tuple[Optional[str], Optional[int]]:
        """Split 'DL2204' into ('DL', 2204). Returns (None, None) if unparseable."""
        flight_ident = flight_ident.strip().upper()
        match = re.match(r'^([A-Z]{2,3})(\d+)$', flight_ident)
        if match:
            return match.group(1), int(match.group(2))
        return None, None

    def get_flight_data(self, flight_ident: str, flight_date: Optional[str] = None,
                        trip_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Smart wrapper: auto-picks /flights/ (live) or /schedules/ (future) based on
        how far out the flight is. Returns the same dict format either way.
        """
        if not flight_date:
            # No date — can only use live endpoint
            return self.get_flight_info(flight_ident, flight_date=flight_date, trip_type=trip_type)

        try:
            target = datetime.strptime(flight_date, '%Y-%m-%d').date()
        except ValueError:
            return self.get_flight_info(flight_ident, flight_date=flight_date, trip_type=trip_type)

        now = django_timezone.now().astimezone(ZoneInfo('America/New_York'))
        # Use start-of-day for the threshold: how many days from today to the target?
        # This keeps flights within ~2 days on /flights/ (live data with real status)
        # and only pushes 3+ days out to /schedules/ (schedule-only, always "Scheduled").
        hours_until = (datetime.combine(target, datetime.min.time()) - datetime.combine(now.date(), datetime.min.time())).total_seconds() / 3600

        if hours_until > self.SCHEDULE_THRESHOLD_HOURS:
            logger.info(f"Flight {flight_ident} is {hours_until:.0f}h away — using /schedules/ endpoint")
            result = self.get_scheduled_flight(flight_ident, flight_date, trip_type=trip_type)
            if result.get('status') == 'success':
                return result
            # Fall back to live endpoint if schedules fails
            logger.warning(f"Schedules endpoint failed for {flight_ident}, falling back to /flights/")

        result = self.get_flight_info(flight_ident, flight_date=flight_date, trip_type=trip_type)

        # If /flights/ returned not_found (e.g. date matching failed), try /schedules/
        # so we at least get schedule data rather than leaving status stale.
        if result.get('status') == 'not_found':
            logger.info(f"Flight {flight_ident} not found via /flights/, trying /schedules/ fallback")
            sched_result = self.get_scheduled_flight(flight_ident, flight_date, trip_type=trip_type)
            if sched_result.get('status') == 'success':
                return sched_result

        return result

    def get_scheduled_flight(self, flight_ident: str, flight_date: str,
                             trip_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetch scheduled flight data using /schedules/{date_start}/{date_end}.
        Works up to 1 year in the future. Returns same dict format as get_flight_info().
        """
        if not self.api_key:
            return {'error': 'AeroAPI key not configured', 'status': 'error'}

        flight_ident = flight_ident.strip().upper()
        airline, flight_number = self._split_flight_ident(flight_ident)
        if not airline or not flight_number:
            return {'error': f'Cannot parse flight ident: {flight_ident}', 'status': 'error'}

        try:
            target = datetime.strptime(flight_date, '%Y-%m-%d').date()
        except ValueError:
            return {'error': f'Invalid date format: {flight_date}', 'status': 'error'}

        # date_end must be +2 days: AeroAPI uses UTC dates, and a US evening departure
        # (e.g. 8:40 PM EDT on Mar 17 = 00:40 AM UTC Mar 18) falls outside a +1 window.
        date_start = target.isoformat()
        date_end = (target + timedelta(days=2)).isoformat()

        # Build query params — filter by airline + flight_number only.
        # We deliberately do NOT add a destination/origin filter: airline +
        # flight_number + date is already unique, and the AeroAPI param
        # accepts only one airport code. Filtering by MCO alone would drop
        # carriers like Allegiant that operate out of SFB (or LAL).
        # Direction is enforced downstream when we match against
        # ORLANDO_AIRPORT_CODES in _parse_scheduled_data.
        params = {
            'airline': airline,
            'flight_number': flight_number,
            'include_codeshares': 'false',
            'max_pages': 1,
        }

        try:
            url = f"{self.base_url}/schedules/{date_start}/{date_end}"

            logger.info(f"Fetching scheduled flight {airline}{flight_number} for {flight_date} from /schedules/")
            response = self.session.get(url, params=params, timeout=10)

            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', '60'))
                return {
                    'error': f'Rate limit exceeded. Please wait {retry_after} seconds.',
                    'status': 'rate_limited',
                    'retry_after': retry_after,
                    'flight_ident': flight_ident,
                }

            if response.status_code == 404:
                return {'error': f'No schedules found for {flight_ident}', 'status': 'not_found'}

            response.raise_for_status()
            data = response.json()

            scheduled_flights = data.get('scheduled', [])
            if not scheduled_flights:
                return {'error': f'No scheduled flights found for {flight_ident} on {flight_date}', 'status': 'not_found'}

            # Select the correct flight from the results.
            # The request ident may be ICAO (e.g. JBU351) while ident_iata in the
            # response is the IATA code (B6351), so ident string matching is
            # unreliable.  Instead, pick by departure date in Eastern time: the
            # flight whose scheduled_out converts to `target` in Eastern is the one
            # the customer booked, regardless of what the ident strings say.
            eastern = ZoneInfo('America/New_York')

            def _depart_date_eastern(sched):
                """Return the Eastern date of scheduled_out, or None."""
                sout = sched.get('scheduled_out')
                if not sout:
                    return None
                try:
                    dt = datetime.fromisoformat(sout.replace('Z', '+00:00'))
                    return dt.astimezone(eastern).date()
                except Exception:
                    return None

            def _airport_code(value):
                """Normalize a schedule's origin/destination to a bare IATA code (KMCO → MCO)."""
                code = (value or '').strip().upper()
                if len(code) == 4 and code.startswith('K'):
                    code = code[1:]
                return code

            def _orlando_match(sched):
                """
                True iff this scheduled flight touches MCO/SFB/LAL in the direction
                that matches the leg's trip_type. For arrivals we require destination,
                for returns we require origin, for unknown trip types either side works.
                """
                orig = _airport_code(sched.get('origin_iata') or sched.get('origin'))
                dest = _airport_code(sched.get('destination_iata') or sched.get('destination'))
                if trip_type == 'arrival':
                    return dest in ORLANDO_AIRPORT_CODES
                if trip_type == 'return':
                    return orig in ORLANDO_AIRPORT_CODES
                return dest in ORLANDO_AIRPORT_CODES or orig in ORLANDO_AIRPORT_CODES

            # Drop any non-Orlando results first — AeroAPI's /schedules/ endpoint
            # returns every flight matching airline+number across the date window,
            # which can include unrelated routes (e.g. AA5921 ROA→CLT shares the
            # number with an MCO arrival on a different day). Saving one of those
            # to a leg would produce phantom flight data.
            orlando_flights = [s for s in scheduled_flights if _orlando_match(s)]
            if not orlando_flights:
                # Build a list of where this flight number actually operates, so
                # the dispatcher knows *why* it was rejected (almost always: the
                # guest booked under a wrong/old flight number). Cap the list to
                # avoid huge messages for connecting carriers.
                found_pairs = []
                seen_pair = set()
                for s in scheduled_flights:
                    orig = _airport_code(s.get('origin_iata') or s.get('origin'))
                    dest = _airport_code(s.get('destination_iata') or s.get('destination'))
                    if not orig and not dest:
                        continue
                    key = f"{orig}|{dest}"
                    if key in seen_pair:
                        continue
                    seen_pair.add(key)
                    found_pairs.append(f"{orig or '?'} → {dest or '?'}")
                    if len(found_pairs) >= 3:
                        break
                routes_str = ", ".join(found_pairs) if found_pairs else "another route"
                msg = (
                    f"{flight_ident} exists, but it flies {routes_str} — "
                    f"not Orlando (MCO/SFB). The reservation likely has the wrong flight number."
                )
                logger.warning(
                    f"Schedules: {flight_ident} returned {len(scheduled_flights)} result(s), "
                    f"none touch MCO/SFB/LAL for trip_type={trip_type}. Routes: {routes_str}"
                )
                return {'error': msg, 'status': 'not_orlando'}

            # Within the Orlando set, prefer the one that departs on the target
            # Eastern date; fall back to the soonest later flight; last resort
            # the first match. Same logic as before, just on the filtered list.
            on_target = [s for s in orlando_flights if _depart_date_eastern(s) == target]
            if on_target:
                best = on_target[0]
                logger.info(f"Schedules: selected Orlando flight departing {target} Eastern (of {len(orlando_flights)} Orlando results)")
            else:
                after_target = sorted(
                    [s for s in orlando_flights if (_depart_date_eastern(s) or date.min) > target],
                    key=lambda s: _depart_date_eastern(s) or date.max,
                )
                if after_target:
                    best = after_target[0]
                    logger.warning(f"Schedules: no Orlando flight departing {target} Eastern, using next available: {_depart_date_eastern(best)}")
                else:
                    best = orlando_flights[0]
                    logger.warning(f"Schedules: falling back to first Orlando result for {flight_ident}")

            return self._parse_scheduled_data(best, trip_type=trip_type)

        except requests.exceptions.RequestException as e:
            logger.error(f"AeroAPI /schedules/ request failed: {e}")
            return {'error': f'API request failed: {str(e)}', 'status': 'error'}
        except Exception as e:
            logger.error(f"Unexpected error in get_scheduled_flight: {e}", exc_info=True)
            return {'error': f'Unexpected error: {str(e)}', 'status': 'error'}

    def _parse_scheduled_data(self, data: Dict, trip_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Parse /schedules/ response into the same dict format as _parse_flight_data().
        Only scheduled times are available — no estimated/actual, no gate/terminal/baggage.
        """
        eastern = ZoneInfo('America/New_York')

        def parse_utc_to_eastern(utc_str):
            if not utc_str:
                return None
            try:
                dt = datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
                return dt.astimezone(eastern)
            except (ValueError, TypeError):
                return None

        # Airport codes from schedules are flat strings like "MCO", "KMCO"
        origin_iata = data.get('origin_iata', '') or data.get('origin', '')
        dest_iata = data.get('destination_iata', '') or data.get('destination', '')
        # Strip ICAO prefix if it looks like one (KMCO → MCO)
        if len(origin_iata) == 4 and origin_iata.startswith('K'):
            origin_iata = origin_iata[1:]
        if len(dest_iata) == 4 and dest_iata.startswith('K'):
            dest_iata = dest_iata[1:]

        origin_str = origin_iata
        dest_str = dest_iata

        is_arrival = dest_iata in ORLANDO_AIRPORT_CODES
        is_departure = origin_iata in ORLANDO_AIRPORT_CODES

        # Parse scheduled times
        scheduled_in = parse_utc_to_eastern(data.get('scheduled_in'))   # gate arrival
        scheduled_out = parse_utc_to_eastern(data.get('scheduled_out')) # gate departure

        # Map to our standard fields based on trip type
        scheduled_gate_arrival = None
        scheduled_runway_arrival = None

        if trip_type == 'arrival' or (trip_type != 'return' and is_arrival):
            # Arrival at Orlando — scheduled_in is gate arrival at MCO
            scheduled_gate_arrival = scheduled_in
            # No runway time available from schedules, use gate time as fallback
            scheduled_runway_arrival = scheduled_in
        elif trip_type == 'return' or is_departure:
            # Departure from Orlando — scheduled_out is gate departure from MCO
            scheduled_gate_arrival = scheduled_out
            scheduled_runway_arrival = scheduled_out
        else:
            # Default to arrival times
            scheduled_gate_arrival = scheduled_in
            scheduled_runway_arrival = scheduled_in

        flight_iata = data.get('ident_iata', '') or data.get('actual_ident_iata', '') or data.get('ident', '')

        result = {
            'status': 'success',
            'flight_iata': flight_iata,
            'origin': origin_str,
            'destination': dest_str,
            'flight_status': 'Scheduled',
            'data_source': 'schedules',  # Lets callers know this is schedule-only data

            # Gate departure in Eastern — lets callers verify the selected schedule
            # actually departs on the date they asked for (get_scheduled_flight falls
            # back to the next available departure when the target date has none).
            'scheduled_departure_local': scheduled_out,

            'scheduled_runway_arrival_local': scheduled_runway_arrival,
            'estimated_runway_arrival_local': None,
            'actual_runway_arrival_local': None,

            'scheduled_gate_arrival_local': scheduled_gate_arrival,
            'estimated_gate_arrival_local': None,
            'actual_gate_arrival_local': None,

            'scheduled_arrival_local': scheduled_runway_arrival,
            'estimated_arrival_local': None,

            'cancelled': False,
            'diverted': False,
            'progress_percent': None,

            'terminal': None,
            'gate': None,
            'baggage_claim': None,
            'last_updated': django_timezone.now(),
        }

        # Add destination arrival times for return trips
        if trip_type == 'return' or is_departure:
            result['scheduled_dest_arrival_local'] = scheduled_in
            result['estimated_dest_arrival_local'] = None
            result['actual_dest_arrival_local'] = None
            result['scheduled_dest_gate_arrival_local'] = scheduled_in
            result['estimated_dest_gate_arrival_local'] = None
            result['actual_dest_gate_arrival_local'] = None

        return result

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

            # Note: /flights/{ident} endpoint doesn't accept date parameter
            # It returns a list of flights, we'll filter by date in the response
            if flight_date:
                logger.info(f"Fetching flight info for {flight_ident} (will filter for date {flight_date}) from AeroAPI")
            else:
                logger.info(f"Fetching flight info for {flight_ident} from AeroAPI")

            response = self.session.get(url, timeout=10)
            
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
            
            # AeroAPI returns flights in a 'flights' array; get first flight involving MCO or SFB (Orlando-area)
            if isinstance(data, dict) and 'flights' in data:
                flights = data.get('flights', [])
                if not flights:
                    return {
                        'error': 'No flights found in response',
                        'status': 'not_found'
                    }
                
                # Filter to find flights involving Orlando-area airports (MCO, SFB) and matching date if provided
                target_date = None
                if flight_date:
                    try:
                        from datetime import datetime as dt
                        target_date = dt.strptime(flight_date, '%Y-%m-%d').date()
                    except ValueError:
                        logger.warning(f"Invalid date format: {flight_date}, ignoring date filter")
                
                candidates = []
                
                desired_direction = None
                if trip_type == "arrival":
                    desired_direction = "arrival"
                elif trip_type == "return":
                    desired_direction = "departure"

                for flight in flights:
                    origin = flight.get('origin', {})
                    destination = flight.get('destination', {})
                    
                    if not isinstance(origin, dict) or not isinstance(destination, dict):
                        continue
                    
                    # Must be arriving at or departing from an Orlando-area airport (MCO or SFB)
                    origin_code = origin.get('code_iata', '')
                    dest_code = destination.get('code_iata', '')
                    
                    if dest_code not in ORLANDO_AIRPORT_CODES and origin_code not in ORLANDO_AIRPORT_CODES:
                        continue
                    
                    # Determine if this is an arrival or departure (at/from MCO or SFB)
                    is_arrival = dest_code in ORLANDO_AIRPORT_CODES
                    is_departure = origin_code in ORLANDO_AIRPORT_CODES

                    # Respect trip type: arrival legs only consider arrivals, return legs only consider departures
                    if desired_direction == "arrival" and not is_arrival:
                        continue
                    if desired_direction == "departure" and not is_departure:
                        continue
                    
                    # If we have a target date, check if this flight matches
                    if target_date:
                        # For arrivals, check scheduled_on (runway) or scheduled_in (gate)
                        # For departures, check scheduled_off (runway) or scheduled_out (gate)
                        scheduled_time = None

                        if is_arrival:
                            scheduled_time = flight.get('scheduled_on') or flight.get('scheduled_in')
                        elif is_departure:
                            scheduled_time = flight.get('scheduled_off') or flight.get('scheduled_out')

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
                                logger.info(f"Found Orlando-area flight candidate for {target_date}: {flight.get('ident_iata', 'Unknown')} {flight_type} {scheduled_dt}")
                    else:
                        # No date filter, just collect Orlando-area flights
                        candidates.append((None, flight))
                
                # Pick the best match
                if candidates:
                    if target_date and len(candidates) > 1:
                        # Sort by scheduled time, but prefer future flights over past flights
                        # For future dates, pick the earliest flight on that date
                        # For past dates, pick the latest flight on that date
                        now = django_timezone.now()
                        now_eastern = now.astimezone(ZoneInfo('America/New_York'))

                        def sort_key(candidate):
                            scheduled_dt = candidate[0]
                            if not scheduled_dt:
                                return datetime.max.replace(tzinfo=ZoneInfo('America/New_York'))

                            # If scheduled time is in the future, prioritize it
                            # If scheduled time is in the past, deprioritize it
                            if scheduled_dt > now_eastern:
                                # Future flight - use negative time to prioritize (earlier = better)
                                return scheduled_dt
                            else:
                                # Past flight - add large offset to deprioritize
                                return scheduled_dt.replace(year=2100)

                        candidates.sort(key=sort_key)
                        logger.info(f"Found {len(candidates)} candidates for {target_date}, selected: {candidates[0][0] if candidates[0][0] else 'unknown time'}")
                    flight_data = candidates[0][1]
                    logger.info(f"Selected flight: {flight_data.get('ident_iata', 'Unknown')}")
                elif target_date:
                    # No flight found for the target date. Do NOT fall back to a
                    # different day's instance — recurring flights (e.g. WN4744 daily)
                    # would silently use a past day's data, causing wrong badges like
                    # "Coming 72 hr early".
                    logger.warning(
                        f"No Orlando-area flight found for {flight_ident} on {target_date} "
                        f"via /flights/ endpoint ({len(flights)} total flights returned). "
                        f"No fallback used — returning not_found."
                    )
                    return {
                        'error': f'No flight found for {flight_ident} on {target_date}',
                        'status': 'not_found',
                    }
                else:
                    # No target_date filter — use first matching Orlando-area flight
                    orlando_fallback = None
                    for flight in flights:
                        origin = flight.get('origin', {})
                        destination = flight.get('destination', {})
                        origin_code = origin.get('code_iata', '') if isinstance(origin, dict) else ''
                        dest_code = destination.get('code_iata', '') if isinstance(destination, dict) else ''

                        is_arrival = dest_code in ORLANDO_AIRPORT_CODES
                        is_departure = origin_code in ORLANDO_AIRPORT_CODES

                        if desired_direction == "arrival" and not is_arrival:
                            continue
                        if desired_direction == "departure" and not is_departure:
                            continue

                        if origin_code in ORLANDO_AIRPORT_CODES or dest_code in ORLANDO_AIRPORT_CODES:
                            orlando_fallback = flight
                            break

                    if orlando_fallback:
                        flight_data = orlando_fallback
                        logger.warning(f"No date filter, using first Orlando-area match: {flight_data.get('ident_iata', 'Unknown')}")
                    else:
                        if desired_direction:
                            return {
                                'error': f'No Orlando-area (MCO/SFB) {desired_direction} flight found',
                                'status': 'not_found'
                            }
                        # Last resort: use first flight
                        flight_data = flights[0]
                        logger.warning(f"No Orlando-area flight found, using first flight: {flight_data.get('ident_iata', 'Unknown')}")
            elif isinstance(data, dict):
                # Single flight object (shouldn't happen but handle it)
                # Verify it's an Orlando-area flight (MCO or SFB)
                origin = data.get('origin', {})
                destination = data.get('destination', {})
                origin_code = origin.get('code_iata', '') if isinstance(origin, dict) else ''
                dest_code = destination.get('code_iata', '') if isinstance(destination, dict) else ''
                
                if origin_code not in ORLANDO_AIRPORT_CODES and dest_code not in ORLANDO_AIRPORT_CODES:
                    return {
                        'error': f'Flight does not involve Orlando-area airport (Origin: {origin_code}, Destination: {dest_code})',
                        'status': 'not_orlando'
                    }
                
                flight_data = data
            elif isinstance(data, list):
                # Direct array of flights
                if not data:
                    return {
                        'error': 'No flights found in response',
                        'status': 'not_found'
                    }
                
                # Filter to find Orlando-area flight (MCO or SFB)
                orlando_flight = None
                for flight in data:
                    origin = flight.get('origin', {})
                    destination = flight.get('destination', {})
                    origin_code = origin.get('code_iata', '') if isinstance(origin, dict) else ''
                    dest_code = destination.get('code_iata', '') if isinstance(destination, dict) else ''
                    
                    if origin_code in ORLANDO_AIRPORT_CODES or dest_code in ORLANDO_AIRPORT_CODES:
                        orlando_flight = flight
                        logger.info(f"Found Orlando-area flight: {flight.get('ident_iata', 'Unknown')} ({origin_code} -> {dest_code})")
                        break
                
                if not orlando_flight:
                    orlando_flight = data[0]
                    logger.warning(f"No Orlando-area flight found, using first flight: {orlando_flight.get('ident_iata', 'Unknown')}")
                
                flight_data = orlando_flight
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
            
            # Determine if this is an arrival at or departure from Orlando-area (MCO or SFB)
            origin_code = origin_data.get('code_iata', '') if isinstance(origin_data, dict) else ''
            dest_code = destination_data.get('code_iata', '') if isinstance(destination_data, dict) else ''
            is_arrival_at_orlando = dest_code in ORLANDO_AIRPORT_CODES
            is_departure_from_orlando = origin_code in ORLANDO_AIRPORT_CODES
            
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
            
            # For arrival trips (arriving at MCO/SFB): use scheduled_on/estimated_on
            # For return trips (departing from MCO/SFB): use scheduled_off/estimated_off
            if trip_type == 'arrival' or (trip_type != 'return' and is_arrival_at_orlando):
                # Arrival at Orlando-area - use arrival times
                scheduled_runway_arrival = parse_and_convert_to_eastern(data.get('scheduled_on'))
                estimated_runway_arrival = parse_and_convert_to_eastern(data.get('estimated_on'))
                actual_runway_arrival = parse_and_convert_to_eastern(data.get('actual_on'))
                
                scheduled_gate_arrival = parse_and_convert_to_eastern(data.get('scheduled_in'))
                estimated_gate_arrival = parse_and_convert_to_eastern(data.get('estimated_in'))
                actual_gate_arrival = parse_and_convert_to_eastern(data.get('actual_in'))
            elif trip_type == 'return' or is_departure_from_orlando:
                # Departure from Orlando-area - use departure times (scheduled_off/estimated_off)
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
            
            def _value_or_tbd(source, key):
                if isinstance(source, dict) and key in source:
                    raw = source.get(key)
                    if raw is None or raw == '':
                        return "TBD"
                    return raw
                return None

            # Get terminal, gate, baggage - prefer top-level fields when present
            terminal = None
            gate = None
            baggage_claim = _value_or_tbd(data, 'baggage_claim')
            
            if trip_type == 'return' or is_departure_from_orlando:
                # For departures, use origin gate/terminal
                terminal = _value_or_tbd(data, 'terminal_origin')
                gate = _value_or_tbd(data, 'gate_origin')
                if (terminal is None) and isinstance(origin_data, dict):
                    terminal = origin_data.get('terminal') or None
                if (gate is None) and isinstance(origin_data, dict):
                    gate = origin_data.get('gate') or None
            else:
                # For arrivals, use destination gate/terminal
                terminal = _value_or_tbd(data, 'terminal_destination')
                gate = _value_or_tbd(data, 'gate_destination')
                if (terminal is None) and isinstance(destination_data, dict):
                    terminal = destination_data.get('terminal') or None
                if (gate is None) and isinstance(destination_data, dict):
                    gate = destination_data.get('gate') or None

            if baggage_claim is None and isinstance(destination_data, dict):
                baggage_claim = destination_data.get('baggage') or None
            
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
                    # Check if flight is in the future - if so, don't mark as "Landed" even if progress is 100
                    # This prevents showing "Landed" for future flights that might have old data
                    now = django_timezone.now().astimezone(ZoneInfo('America/New_York'))
                    is_future = False
                    
                    # Check if scheduled time is in the future (already parsed above)
                    if scheduled_runway_arrival and scheduled_runway_arrival > now:
                        is_future = True
                    elif scheduled_gate_arrival and scheduled_gate_arrival > now:
                        is_future = True
                    
                    # Try to determine from progress
                    progress = data.get('progress_percent')
                    if progress is not None:
                        if progress == 0:
                            status = 'Scheduled'
                        elif progress < 100:
                            status = 'En Route'
                        elif progress >= 100 and not is_future:
                            # Only mark as "Landed" if flight is not in the future
                            status = 'Landed'
                        else:
                            # Progress is 100 but flight is in the future - likely old data, mark as Scheduled
                            status = 'Scheduled'
                    elif is_future:
                        # No progress data but flight is in the future
                        status = 'Scheduled'
                    else:
                        # No progress data and not clearly in the future - default to Scheduled
                        status = 'Scheduled'
            
            result = {
                'status': 'success',  # This indicates parsing was successful
                'flight_iata': data.get('ident_iata', '') or data.get('ident', ''),
                'origin': self._format_airport(origin_data),
                'destination': self._format_airport(destination_data),
                'flight_status': status,  # This is the actual flight status (Scheduled, En Route, etc.)
                
                # Orlando-area times (arrival at or departure from MCO/SFB, depending on trip type)
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
            if trip_type == 'return' or is_departure_from_orlando:
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

