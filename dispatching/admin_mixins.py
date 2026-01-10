"""
Admin mixins for restricting sensitive information from dispatchers.
"""
from django.contrib import admin


class DispatcherAdminMixin:
    """
    Mixin to hide sensitive financial fields from dispatchers (non-superusers).
    Only superusers can see revenue, profit, and commission information.
    """
    
    # Fields to hide from dispatchers (non-superusers)
    SENSITIVE_FIELDS = [
        'total_price',
        'base_price',
        'profit_estimate',
        'profit_percentage',
        'total_driver_payments',
        'commission_amount',
        'commission_paid',
        'total_amount',
        'unpaid_commissions',
        'pending_commissions',
        'total_paid_commissions',
    ]
    
    def get_fieldsets(self, request, obj=None):
        """Remove sensitive fields from fieldsets for dispatchers."""
        fieldsets = super().get_fieldsets(request, obj)
        
        if not request.user.is_superuser:
            # Filter out sensitive fields from fieldsets
            filtered_fieldsets = []
            for name, options in fieldsets:
                if 'fields' in options:
                    # Filter out sensitive fields
                    filtered_fields = [
                        field for field in options['fields']
                        if not any(sensitive in str(field) for sensitive in self.SENSITIVE_FIELDS)
                    ]
                    # Handle tuple fields (like ("total_price", "base_price"))
                    final_fields = []
                    for field in filtered_fields:
                        if isinstance(field, tuple):
                            # Filter tuple fields
                            filtered_tuple = tuple(
                                f for f in field 
                                if not any(sensitive in str(f) for sensitive in self.SENSITIVE_FIELDS)
                            )
                            if filtered_tuple:  # Only add if not empty
                                final_fields.append(filtered_tuple)
                        else:
                            final_fields.append(field)
                    
                    if final_fields:  # Only add fieldset if it has fields
                        filtered_fieldsets.append((name, {**options, 'fields': final_fields}))
                else:
                    filtered_fieldsets.append((name, options))
            
            return tuple(filtered_fieldsets)
        
        return fieldsets
    
    def get_readonly_fields(self, request, obj=None):
        """Add sensitive fields to readonly for dispatchers if they somehow access them."""
        readonly = list(super().get_readonly_fields(request, obj) or [])
        if not request.user.is_superuser:
            readonly.extend(self.SENSITIVE_FIELDS)
        return readonly
    
    def get_list_display(self, request):
        """Remove sensitive fields from list display for dispatchers."""
        list_display = list(super().get_list_display(request))
        if not request.user.is_superuser:
            list_display = [
                field for field in list_display
                if not any(sensitive in str(field) for sensitive in self.SENSITIVE_FIELDS)
            ]
        return list_display
    
    def get_list_filter(self, request):
        """Remove sensitive filters for dispatchers."""
        list_filter = list(super().get_list_filter(request) or [])
        if not request.user.is_superuser:
            list_filter = [
                field for field in list_filter
                if not any(sensitive in str(field) for sensitive in self.SENSITIVE_FIELDS)
            ]
        return list_filter


class DispatcherModelAdmin(DispatcherAdminMixin, admin.ModelAdmin):
    """Base admin class for models that should hide sensitive info from dispatchers."""
    pass
