from django import template
from django.utils.safestring import mark_safe
import json

register = template.Library()


def get_base_structured_data(request, additional_data=None):
    """
    Generate base structured data with optional customization
    """
    base_data = {
        "@context": "https://schema.org",
        "@type": ["Organization", "LocalBusiness"],
        "name": "Grayson Towncar",
        "address": {
            "@type": "PostalAddress",
            "addressLocality": "Orlando",
            "addressRegion": "FL",
            "postalCode": "32827",
            "addressCountry": "US",
        },
        "description": (
            "Premier private airport transportation service in Orlando, offering luxury "
            "MCO airport transfers, Disney resort shuttles, Universal Studios transportation, "
            "and Port Canaveral cruise transfers with exceptional customer service and flexibility."
        ),
        "image": "https://www.graysontowncar.com/static/images/logo.png",
        "logo": "https://www.graysontowncar.com/static/images/logo.png",
        "url": "https://www.graysontowncar.com/",
        "telephone": "+1-407-212-7190",
        "priceRange": "$$",
        "paymentAccepted": ["Credit Card", "Debit Card"],
        "openingHoursSpecification": {
            "@type": "OpeningHoursSpecification",
            "dayOfWeek": [
                "Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday",
            ],
            "opens": "00:00",
            "closes": "23:59",
        },
        "areaServed": [
            {"@type": "City", "name": "Orlando"},
            {"@type": "Airport", "name": "Orlando International Airport (MCO)", "iataCode": "MCO"},
            {"@type": "Place", "name": "Walt Disney World Resort"},
            {"@type": "Place", "name": "Universal Orlando Resort"},
            {"@type": "Place", "name": "Port Canaveral"},
        ],
        "hasMap": "https://maps.google.com/?q=Grayson+Towncar+Orlando+FL",
        "hasOfferCatalog": {
            "@type": "OfferCatalog",
            "name": "Orlando Transportation Vehicle Options",
            "itemListElement": [
                {
                    "@type": "Offer",
                    "itemOffered": {
                        "@type": "Service",
                        "name": "Luxury SUV Transportation",
                        "description": (
                            "Premium SUV airport transportation with professional drivers "
                            "and complimentary meet and greet"
                        ),
                    },
                },
                {
                    "@type": "Offer",
                    "itemOffered": {
                        "@type": "Service",
                        "name": "Executive Towncar Transportation",
                        "description": (
                            "Classic towncar service for elegant and comfortable travel "
                            "to MCO and Orlando attractions"
                        ),
                    },
                },
                {
                    "@type": "Offer",
                    "itemOffered": {
                        "@type": "Service",
                        "name": "Orlando Van Transportation",
                        "description": (
                            "Spacious van service ideal for larger groups and Disney resort transfers"
                        ),
                    },
                },
                {
                    "@type": "Offer",
                    "itemOffered": {
                        "@type": "Service",
                        "name": "Family Mini Van Transportation",
                        "description": (
                            "Comfortable mini van service perfect for families with "
                            "complimentary car seats and booster seats"
                        ),
                    },
                },
            ],
        },
        "aggregateRating": {
            "@type": "AggregateRating",
            "ratingValue": 5.0,
            "bestRating": 5,
            "ratingCount": 1700,
        },
        "potentialAction": {
            "@type": "ReserveAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": "https://www.graysontowncar.com/rates-booking/",
                "inLanguage": "en-US",
            },
        },
        "knowsAbout": [
            "Orlando Airport Transportation",
            "MCO Airport Transfers",
            "Disney World Resort Transportation",
            "Universal Studios Shuttle Service",
            "Port Canaveral Cruise Transfers",
            "Luxury Airport Shuttle",
            "Private Transportation Orlando",
            "Corporate Event Transportation",
            "Family-Friendly Orlando Transportation",
        ],
    }

    if additional_data:
        base_data.update(additional_data)

    return base_data


@register.simple_tag(takes_context=True)
def structured_data(context, page_type="home", additional_data=None):
    """
    Generate structured data for different page types
    """
    request = context["request"]
    data = get_base_structured_data(request, additional_data)

    scripts = f'<script type="application/ld+json">\n{json.dumps(data, indent=2)}\n</script>'

    # Add WebSite schema on the homepage
    if request.path == "/":
        website_data = {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": "Grayson Towncar",
            "url": "https://www.graysontowncar.com",
        }
        scripts += f'\n<script type="application/ld+json">\n{json.dumps(website_data, indent=2)}\n</script>'

    return mark_safe(scripts)
