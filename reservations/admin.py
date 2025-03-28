# admin.py
from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django import forms
from .models import Vehicle, Route, Rate, Reservation, FlightInformation

# Import the import_export functionality if installed
try:
    from import_export.admin import ImportExportModelAdmin
    from import_export import resources
    class RateResource(resources.ModelResource):
        class Meta:
            model = Rate
            fields = ('id', 'vehicle__vehicle_type', 'route__name', 'oneway_price', 'round_trip_price')
            export_order = fields
    
    class VehicleResource(resources.ModelResource):
        class Meta:
            model = Vehicle
            fields = ('id', 'vehicle_type', 'capacity', 'luggage_capacity')

    class RouteResource(resources.ModelResource):
        class Meta:
            model = Route
            fields = ('id', 'name')
            
    BaseModelAdmin = ImportExportModelAdmin
except ImportError:
    BaseModelAdmin = admin.ModelAdmin
    RateResource = None
    VehicleResource = None
    RouteResource = None

# Admin form for inline editing
class RateAdminForm(forms.ModelForm):
    class Meta:
        model = Rate
        fields = '__all__'
        widgets = {
            'oneway_price': forms.NumberInput(attrs={'style': 'width:100px'}),
            'round_trip_price': forms.NumberInput(attrs={'style': 'width:100px'}),
        }

# Inline classes for related models
class RateInline(admin.TabularInline):
    model = Rate
    form = RateAdminForm
    extra = 1
    fields = ('vehicle', 'route', 'oneway_price', 'round_trip_price')
    classes = ('collapse',)

class FlightInformationInline(admin.TabularInline):
    model = FlightInformation
    extra = 1
    fields = ('flight_type', 'airline', 'flight_number', 'date', 'time')

# Main admin classes
@admin.register(Vehicle)
class VehicleAdmin(BaseModelAdmin):
    resource_class = VehicleResource if VehicleResource else None
    list_display = ('vehicle_type_display', 'capacity', 'luggage_capacity', 'image_preview')
    list_filter = ('vehicle_type',)
    search_fields = ('vehicle_type',)
    inlines = [RateInline]
    
    def vehicle_type_display(self, obj):
        return obj.get_vehicle_type_display()
    vehicle_type_display.short_description = 'Vehicle Type'
    
    def image_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" width="100" />', obj.image.url)
        return "No Image"
    image_preview.short_description = 'Preview'

@admin.register(Route)
class RouteAdmin(BaseModelAdmin):
    resource_class = RouteResource if RouteResource else None
    list_display = ('name', 'count_rates')
    search_fields = ('name',)
    inlines = [RateInline]
    
    def count_rates(self, obj):
        count = Rate.objects.filter(route=obj).count()
        return format_html('<a href="{}?route__id__exact={}">{} rates</a>', 
                           reverse('admin:reservations_rate_changelist'), 
                           obj.id, count)
    count_rates.short_description = 'Number of Rates'

@admin.register(Rate)
class RateAdmin(BaseModelAdmin):
    resource_class = RateResource if RateResource else None
    form = RateAdminForm
    list_display = ('id', 'vehicle_display', 'route_display', 'oneway_price', 'round_trip_price', 'edit_link')
    list_filter = ('vehicle__vehicle_type', 'route')
    search_fields = ('vehicle__vehicle_type', 'route__name')
    list_editable = ('oneway_price', 'round_trip_price')
    ordering = ('route__name', 'vehicle__vehicle_type')
    
    fieldsets = (
        (None, {
            'fields': (('vehicle', 'route'),)
        }),
        ('Pricing', {
            'fields': (('oneway_price', 'round_trip_price'),),
            'classes': ('wide',),
        }),
    )
    
    def vehicle_display(self, obj):
        return obj.vehicle.get_vehicle_type_display()
    vehicle_display.short_description = 'Vehicle'
    vehicle_display.admin_order_field = 'vehicle__vehicle_type'
    
    def route_display(self, obj):
        return obj.route.name
    route_display.short_description = 'Route'
    route_display.admin_order_field = 'route__name'
    
    def edit_link(self, obj):
        return format_html(
            '<a class="button" href="{}">Edit</a>',
            reverse('admin:reservations_rate_change', args=[obj.pk])
        )
    edit_link.short_description = 'Actions'

@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ('id', 'full_name', 'trip_type', 'vehicle_display', 'route_display', 
                   'pickup_date', 'payment_status', 'total_price')
    list_filter = ('trip_type', 'vehicle', 'route', 'pickup_date', 'payemnt_status')
    search_fields = ('first_name', 'last_name', 'email', 'phone_number')
    inlines = [FlightInformationInline]
    
    fieldsets = (
        ('Customer Information', {
            'fields': (('first_name', 'last_name'), ('email', 'phone_number'), 'zipcode'),
        }),
        ('Trip Details', {
            'fields': (('trip_type', 'vehicle', 'route'), 
                      ('passenger_count', 'luggage_count', 'has_children'),
                      ('pickup_date', 'pickup_time'),
                      ('pickup_location', 'dropoff_location'))
        }),
        ('Return Trip', {
            'fields': (('return_date', 'return_time'),
                      ('return_pickup_location', 'return_dropoff_location')),
            'classes': ('collapse',),
        }),
        ('Special Requests', {
            'fields': (('carseat_type', 'store_stop'), 'special_requests'),
            'classes': ('collapse',),
        }),
        ('Payment Information', {
            'fields': (('base_price', 'additional_charges', 'total_price'), 
                      ('payemnt_status', 'stripe_payment_id')),
            'classes': ('wide',),
        }),
    )
    
    readonly_fields = ('total_price',)
    
    def full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}"
    full_name.short_description = 'Customer Name'
    
    def vehicle_display(self, obj):
        return obj.vehicle.get_vehicle_type_display()
    vehicle_display.short_description = 'Vehicle'
    
    def route_display(self, obj):
        return obj.route.name
    route_display.short_description = 'Route'
    
    def payment_status(self, obj):
        status = obj.payemnt_status
        if status == 'PENDING':
            return format_html('<span style="color:orange">{}</span>', status)
        elif status == 'COMPLETED':
            return format_html('<span style="color:green">{}</span>', status)
        else:
            return format_html('<span style="color:red">{}</span>', status)
    payment_status.short_description = 'Payment Status'