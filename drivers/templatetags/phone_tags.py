"""``{{ driver.phone_number|phone }}`` → ``(407) 555-0134``.

See drivers/phones.py. ``|tel`` gives the href form (E.164 when parseable)."""
from django import template

from drivers import phones

register = template.Library()


@register.filter(name="phone")
def phone(value):
    return phones.pretty(value)


@register.filter(name="tel")
def tel(value):
    return phones.normalize(value) or phones.digits_only(value)
