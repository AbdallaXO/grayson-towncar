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
        },
        "description": (
            "Premier private airport transportation service in Orlando, offering luxury "
            "MCO airport transfers, Disney resort shuttles, Universal Studios transportation, "
            "and Port Canaveral cruise transfers with exceptional customer service and flexibility."
        ),
        "image": "https://www.graysontowncar.com/logo.jpg",
        "url": request.build_absolute_uri(),
        "telephone": "+1-407-212-7190",
        "priceRange": "$$",
        "award": [
            "Top-Rated Orlando Airport Transportation Service",
            "Customer Choice Award for Orlando Private Transportation",
            "Family-Friendly Disney Resort Transportation Service of the Year",
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
        "additionalType": [
            "Airport Shuttle",
            "Disney Resort Transportation",
            "Corporate Transportation",
            "Wedding Transportation",
            "Port Canaveral Cruise Transfer",
            "Group Transportation",
        ],
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
            "ratingValue": 4.8,
            "bestRating": 5,
            "ratingCount": 250,
        },
        "potentialAction": {
            "@type": "ReserveAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": f"{request.scheme}://{request.get_host()}/rates-booking/",
                "inLanguage": ["en-US", "ar-SA"],
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
            "Wedding Transportation",
            "Family-Friendly Orlando Transportation",
        ],
        "parentOrganization": {
            "@type": "Organization",
            "name": "Grayson Towncar",
            "description": (
                "Providing premium Orlando airport transportation and luxury shuttle "
                "solutions in Central Florida"
            ),
        },
        "keywords": [
            "Orlando airport transfers",
            "MCO transportation service",
            "Disney World transportation",
            "Universal Studios transportation",
            "Port Canaveral cruise transfer",
            "MCO airport to disney world",
            "Luxury airport transfer",
            "Private Orlando transportation",
            "Orlando chauffeur service",
            "Family-friendly transportation Orlando",
            "Meet and greet airport service",
            "Orlando transportation with car seats",
            "Orlando black car service",
            "Orlando luxury ground transportation",
            "Orlando Airport Car Service",
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

    # Add page-specific enhancements here if needed

    return mark_safe(f"""
    <script type=\"application/ld+json\">\n{json.dumps(data, indent=2)}\n    </script>
    """)
