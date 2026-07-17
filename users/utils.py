import os
import logging
from datetime import datetime, timezone
from functools import wraps
from django.core.cache import cache

logger = logging.getLogger(__name__)

# HubSpot integration removed - no longer using HubSpot
# This file can be used for other user-related utilities in the future