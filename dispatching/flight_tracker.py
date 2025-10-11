import requests
import logging
from django.conf import settings
from typing import Dict, Optional, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class FlightTracker:
    """
    Service to track flight status using FlightLabs API for scheduled flights
    """
    
    def __init__(self):
        self.api_key = getattr(settings, 'FLIGHTLABS_API_KEY', None)
        self.base_url = "https://app.goflightlabs.com"
        
    def get_flight_status(self, airline: str, flight_number: str, date: str = None) -> Dict[str, Any]:
        """
        Get scheduled flight status from FlightLabs API
        
        Args:
            airline: Airline code (e.g., 'AA', 'DL', 'WN')
            flight_number: Flight number (e.g., '1234')
            date: Flight date in YYYY-MM-DD format (optional, defaults to today)
            
        Returns:
            Dict containing flight status information
        """
        if not self.api_key:
            return {
                'error': 'FlightLabs API key not configured',
                'status': 'error'
            }
        
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
            
        # Clean airline code (remove spaces, convert to uppercase, handle common airline names)
        airline = airline.strip().upper()
        flight_number = flight_number.strip()
        
        # Convert common airline names to IATA codes
        airline_mapping = {
            'UNITED AIRLINES': 'UA',
            'AMERICAN AIRLINES': 'AA', 
            'DELTA AIRLINES': 'DL',
            'SOUTHWEST AIRLINES': 'WN',
            'JETBLUE': 'B6',
            'SPIRIT AIRLINES': 'NK',
            'FRONTIER AIRLINES': 'F9',
            'ALASKA AIRLINES': 'AS',
            'HAWAIIAN AIRLINES': 'HA',
            'ALLEGIANT AIR': 'G4'
        }
        
        # Check if it's a full airline name and convert to IATA code
        if airline in airline_mapping:
            airline = airline_mapping[airline]
        
        # Remove any non-alphanumeric characters from flight number
        flight_number = ''.join(c for c in flight_number if c.isalnum())
        
        try:
            # FlightLabs API endpoint for flight status
            url = f"{self.base_url}/flights"
            params = {
                'access_key': self.api_key,
                'flight_iata': f"{airline}{flight_number}",
                'flight_date': date
            }
            
            logger.info(f"Fetching flight status for {airline}{flight_number} on {date}")
            logger.info(f"API URL: {url}")
            logger.info(f"API Params: {params}")
            
            response = requests.get(url, params=params, timeout=10)
            logger.info(f"API Response Status: {response.status_code}")
            logger.info(f"API Response Text: {response.text[:500]}")  # Log first 500 chars
            
            response.raise_for_status()
            
            data = response.json()
            
            if data and len(data) > 0:
                # Get the first flight result
                flight_data = data[0]
                return self._parse_aviation_edge_data(flight_data)
            else:
                return {
                    'error': 'No flight data found',
                    'status': 'not_found',
                    'flight': f"{airline}{flight_number}",
                    'date': date
                }
                
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
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
    
    def _parse_aviation_edge_data(self, flight_data: Dict) -> Dict[str, Any]:
        """
        Parse flight data from Aviation Edge API response (scheduled flight data)
        """
        try:
            # Aviation Edge provides scheduled flight information
            result = {
                'status': 'success',
                'flight': flight_data.get('flight', {}).get('iata', 'Unknown'),
                'airline': flight_data.get('airline', {}).get('name', 'Unknown'),
                'aircraft': flight_data.get('aircraft', {}).get('iata', 'Unknown'),
                'departure': {
                    'airport': flight_data.get('departure', {}).get('airport', 'Unknown'),
                    'scheduled': flight_data.get('departure', {}).get('scheduled', 'Unknown'),
                    'actual': flight_data.get('departure', {}).get('actual', 'Unknown'),
                    'gate': flight_data.get('departure', {}).get('gate', 'Unknown'),
                    'terminal': flight_data.get('departure', {}).get('terminal', 'Unknown'),
                    'delay': flight_data.get('departure', {}).get('delay', 0)
                },
                'arrival': {
                    'airport': flight_data.get('arrival', {}).get('airport', 'Unknown'),
                    'scheduled': flight_data.get('arrival', {}).get('scheduled', 'Unknown'),
                    'actual': flight_data.get('arrival', {}).get('actual', 'Unknown'),
                    'gate': flight_data.get('arrival', {}).get('gate', 'Unknown'),
                    'terminal': flight_data.get('arrival', {}).get('terminal', 'Unknown'),
                    'delay': flight_data.get('arrival', {}).get('delay', 0)
                },
                'status_text': flight_data.get('status', 'Unknown'),
                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'data_type': 'scheduled'
            }
            
            # Calculate delay status for arrival
            arrival_delay = result['arrival']['delay']
            if arrival_delay > 0:
                result['delay_status'] = 'delayed'
                result['delay_minutes'] = arrival_delay
            elif arrival_delay < 0:
                result['delay_status'] = 'early'
                result['delay_minutes'] = abs(arrival_delay)
            else:
                result['delay_status'] = 'on_time'
                result['delay_minutes'] = 0
                
            return result
            
        except Exception as e:
            logger.error(f"Error parsing Aviation Edge flight data: {e}")
            return {
                'error': f'Error parsing flight data: {str(e)}',
                'status': 'error'
            }

    def _parse_flight_data(self, flight_data: Dict) -> Dict[str, Any]:
        """
        Parse flight data from FlightLabs API response (ADS-B live tracking data)
        """
        try:
            # FlightLabs API returns live tracking data, not scheduled flight info
            # We'll extract what we can and provide a note about the data type
            
            result = {
                'status': 'success',
                'flight': flight_data.get('flight_iata', 'Unknown'),
                'airline': flight_data.get('airline_iata', 'Unknown'),
                'aircraft': flight_data.get('aircraft_icao', 'Unknown'),
                'departure': {
                    'airport': flight_data.get('dep_iata', 'Unknown'),
                    'scheduled': 'Live tracking data',
                    'actual': 'Live tracking data',
                    'gate': 'Not available in live data',
                    'terminal': 'Not available in live data',
                    'delay': 0
                },
                'arrival': {
                    'airport': flight_data.get('arr_iata', 'Unknown'),
                    'scheduled': 'Live tracking data',
                    'actual': 'Live tracking data',
                    'gate': 'Not available in live data',
                    'terminal': 'Not available in live data',
                    'delay': 0
                },
                'status_text': flight_data.get('status', 'Unknown'),
                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'data_type': 'live_tracking',
                'live_data': {
                    'latitude': flight_data.get('lat'),
                    'longitude': flight_data.get('lng'),
                    'altitude': flight_data.get('alt'),
                    'speed': flight_data.get('speed'),
                    'direction': flight_data.get('dir'),
                    'vertical_speed': flight_data.get('v_speed'),
                    'registration': flight_data.get('reg_number'),
                    'flag': flight_data.get('flag')
                }
            }
            
            # For live tracking data, we can't determine delays from scheduled times
            # But we can show the flight status
            if result['status_text'] == 'en-route':
                result['delay_status'] = 'in_flight'
                result['delay_minutes'] = 0
            elif result['status_text'] == 'landed':
                result['delay_status'] = 'landed'
                result['delay_minutes'] = 0
            else:
                result['delay_status'] = 'unknown'
                result['delay_minutes'] = 0
                
            return result
            
        except Exception as e:
            logger.error(f"Error parsing flight data: {e}")
            return {
                'error': f'Error parsing flight data: {str(e)}',
                'status': 'error'
            }
    
    def get_mco_arrivals(self, date: str = None) -> Dict[str, Any]:
        """
        Get all arrivals for MCO on a specific date
        
        Args:
            date: Date in YYYY-MM-DD format (optional, defaults to today)
            
        Returns:
            Dict containing all MCO arrivals
        """
        if not self.api_key:
            return {
                'error': 'FlightLabs API key not configured',
                'status': 'error'
            }
        
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
            
        try:
            url = f"{self.base_url}/flights"
            params = {
                'access_key': self.api_key,
                'arr_iata': 'MCO',
                'flight_date': date
            }
            
            logger.info(f"Fetching MCO arrivals for {date}")
            logger.info(f"API URL: {url}")
            logger.info(f"API Params: {params}")
            
            response = requests.get(url, params=params, timeout=10)
            logger.info(f"API Response Status: {response.status_code}")
            logger.info(f"API Response Text: {response.text[:500]}")  # Log first 500 chars
            
            response.raise_for_status()
            
            data = response.json()
            
            if 'data' in data and data['data']:
                arrivals = []
                for flight in data['data']:
                    arrivals.append(self._parse_flight_data(flight))
                
                return {
                    'status': 'success',
                    'arrivals': arrivals,
                    'count': len(arrivals),
                    'date': date
                }
            else:
                return {
                    'error': 'No arrival data found',
                    'status': 'not_found',
                    'date': date
                }
                
        except Exception as e:
            logger.error(f"Error fetching MCO arrivals: {e}")
            return {
                'error': f'Error fetching arrivals: {str(e)}',
                'status': 'error'
            }

    def get_mco_departures(self, date: str = None) -> Dict[str, Any]:
        """
        Get all MCO departures for a specific date using FlightLabs API
        
        Args:
            date: Date in YYYY-MM-DD format (optional, defaults to today)
            
        Returns:
            Dict containing all MCO departures
        """
        if not self.api_key:
            return {
                'error': 'FlightLabs API key not configured',
                'status': 'error'
            }
        
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
            
        try:
            # FlightLabs API endpoint for airport departures
            url = f"{self.base_url}/flights"
            params = {
                'access_key': self.api_key,
                'dep_iata': 'MCO',
                'flight_date': date
            }
            
            logger.info(f"Fetching MCO departures for {date}")
            logger.info(f"API URL: {url}")
            logger.info(f"API Params: {params}")
            
            response = requests.get(url, params=params, timeout=10)
            logger.info(f"API Response Status: {response.status_code}")
            logger.info(f"API Response Text: {response.text[:500]}")  # Log first 500 chars
            
            response.raise_for_status()
            
            data = response.json()
            
            if 'data' in data and data['data']:
                departures = []
                for flight in data['data']:
                    departures.append(self._parse_flight_data(flight))
                
                return {
                    'status': 'success',
                    'departures': departures,
                    'count': len(departures),
                    'date': date
                }
            else:
                return {
                    'error': 'No departure data found',
                    'status': 'not_found',
                    'date': date
                }
                
        except Exception as e:
            logger.error(f"Error fetching MCO departures: {e}")
            return {
                'error': f'Error fetching departures: {str(e)}',
                'status': 'error'
            }


# Alternative API implementations for comparison
class AviationEdgeTracker:
    """
    Alternative flight tracker using Aviation Edge API
    """
    
    def __init__(self):
        self.api_key = getattr(settings, 'AVIATION_EDGE_API_KEY', None)
        self.base_url = "https://aviation-edge.com/v2/public"
        
    def get_flight_status(self, airline: str, flight_number: str, date: str = None) -> Dict[str, Any]:
        """
        Get flight status using Aviation Edge API
        """
        if not self.api_key:
            return {
                'error': 'Aviation Edge API key not configured',
                'status': 'error'
            }
        
        try:
            url = f"{self.base_url}/flightstatus"
            params = {
                'key': self.api_key,
                'flightIata': f"{airline}{flight_number}",
                'flightDate': date or datetime.now().strftime('%Y-%m-%d')
            }
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            if data:
                return self._parse_aviation_edge_data(data[0])
            else:
                return {
                    'error': 'No flight data found',
                    'status': 'not_found'
                }
                
        except Exception as e:
            logger.error(f"Aviation Edge API error: {e}")
            return {
                'error': f'API error: {str(e)}',
                'status': 'error'
            }
    
    def _parse_aviation_edge_data(self, flight_data: Dict) -> Dict[str, Any]:
        """
        Parse Aviation Edge API response
        """
        return {
            'status': 'success',
            'flight': flight_data.get('flight', {}).get('iata', 'Unknown'),
            'airline': flight_data.get('airline', {}).get('name', 'Unknown'),
            'departure': {
                'airport': flight_data.get('departure', {}).get('airport', 'Unknown'),
                'scheduled': flight_data.get('departure', {}).get('scheduled', 'Unknown'),
                'actual': flight_data.get('departure', {}).get('actual', 'Unknown'),
                'gate': flight_data.get('departure', {}).get('gate', 'Unknown'),
                'delay': flight_data.get('departure', {}).get('delay', 0)
            },
            'arrival': {
                'airport': flight_data.get('arrival', {}).get('airport', 'Unknown'),
                'scheduled': flight_data.get('arrival', {}).get('scheduled', 'Unknown'),
                'actual': flight_data.get('arrival', {}).get('actual', 'Unknown'),
                'gate': flight_data.get('arrival', {}).get('gate', 'Unknown'),
                'delay': flight_data.get('arrival', {}).get('delay', 0)
            },
            'status_text': flight_data.get('status', 'Unknown'),
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
