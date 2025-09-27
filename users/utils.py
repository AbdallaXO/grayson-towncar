import os
import logging
from datetime import datetime, timezone
from functools import wraps
from django.core.cache import cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# HubSpot integration removed - no longer using HubSpot
# This file can be used for other user-related utilities in the future