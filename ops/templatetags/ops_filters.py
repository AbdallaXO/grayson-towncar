from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Look up a key in a dict. Returns 0 if not found."""
    if isinstance(dictionary, dict):
        return dictionary.get(key, 0)
    return 0


@register.filter
def trend_key(uid, metric):
    """Build a trend lookup key: {{ uid|trend_key:'completions' }}"""
    return f"{uid}_{metric}"


@register.filter
def get_trend(trends_dict, key):
    """Look up a trend entry. Returns dict with direction, pct, prior."""
    if isinstance(trends_dict, dict):
        return trends_dict.get(key)
    return None
