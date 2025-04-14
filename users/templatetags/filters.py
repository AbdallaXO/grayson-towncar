from django import template

register = template.Library()


@register.filter
def format_status(value):
    return value.replace("_", " ").title()
