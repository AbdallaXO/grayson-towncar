# rates/templatetags/seo_tags.py
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
        "@type": "TransportService",
        "name": "Grayson Towncar",
        "description": "Premier private transportation service in Orlando, offering comprehensive transportation solutions with exceptional customer service and flexibility.",
        "image": "https://www.graysontowncar.com/logo.jpg",
        "url": request.build_absolute_uri(),
        "telephone": "+1-407-212-7190",
        "priceRange": "$$",
        "award": [
            "Top-Rated Orlando Transportation Service",
            "Customer Choice Award for Orlando Transportation",
            "Family-Friendly Transportation Service of the Year",
        ],
        "areaServed": {
            "@type": "Place",
            "name": "Orlando Metropolitan Area",
            "geo": {
                "@type": "GeoCircle",
                "geoMidpoint": {
                    "@type": "GeoCoordinates",
                    "latitude": 28.5383,
                    "longitude": -81.3792,
                },
                "geoRadius": "100 mi",
            },
        },
        "languages": ["en-US", "ar-SA"],
        "additionalType": [
            "Airport Shuttle",
            "Corporate Transportation",
            "Wedding Transportation",
            "Group Transportation",
        ],
        "hasOfferCatalog": {
            "@type": "OfferCatalog",
            "name": "Transportation Vehicle Options",
            "itemListElement": [
                {
                    "@type": "Offer",
                    "itemOffered": {
                        "@type": "Service",
                        "name": "SUV Transportation",
                        "description": "Luxury SUV transportation with professional drivers",
                    },
                },
                {
                    "@type": "Offer",
                    "itemOffered": {
                        "@type": "Service",
                        "name": "Towncar Transportation",
                        "description": "Classic towncar service for elegant and comfortable travel",
                    },
                },
                {
                    "@type": "Offer",
                    "itemOffered": {
                        "@type": "Service",
                        "name": "Van Transportation",
                        "description": "Spacious van service ideal for larger groups",
                    },
                },
                {
                    "@type": "Offer",
                    "itemOffered": {
                        "@type": "Service",
                        "name": "Mini Van Transportation",
                        "description": "Comfortable mini van service perfect for families",
                    },
                },
            ],
        },
        "serviceFeatures": [
            "Free grocery store stops",
            "Complimentary car seats and booster seats",
            "MCO airport meet and greet",
            "24/7 pickup availability",
            "Flight monitoring",
            "Multilingual service",
        ],
        "aggregateRating": {
            "@type": "AggregateRating",
            "ratingValue": "4.8",
            "reviewCount": "250",
        },
        "potentialAction": {
            "@type": "ReserveAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": f"{request.scheme}://{request.get_host()}/rates/",
                "inLanguage": ["en-US", "ar-SA"],
            },
        },
        "knowsAbout": [
            "Orlando Airport Transportation",
            "Disney Resort Transfers",
            "Universal Studios Transportation",
            "Port Canaveral Cruise Transfers",
            "Corporate Event Transportation",
            "Wedding Transportation",
        ],
        "parentOrganization": {
            "@type": "Organization",
            "name": "Grayson Towncar Services",
            "description": "Providing premium transportation solutions in Central Florida",
        },
        "areaServedDetails": {
            "@type": "Place",
            "name": "Central Florida Transportation Network",
            "description": "Serving Orlando, Disney World, Universal Studios, Port Canaveral, and surrounding areas within a 100-mile radius",
        },
        "keywords": [
            "Orlando airport shuttle",
            "Disney transportation",
            "Universal Studios transportation",
            "Orlando towncar",
            "Private airport transfer",
            "Multilingual transportation",
            "Corporate transportation",
            "Wedding transportation",
            "Group transportation",
        ],
    }

    # Allow for additional or overriding data
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

    return mark_safe(f"""
    <script type="application/ld+json">
    {json.dumps(data, indent=2)}
    </script>
    """)
