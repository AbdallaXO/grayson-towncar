from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Look up a key in a dict. Returns 0 if not found."""
    if isinstance(dictionary, dict):
        return dictionary.get(key, 0)
    return 0
