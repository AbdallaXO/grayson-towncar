from django.apps import AppConfig

class PaymentConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'payment'
    
    def ready(self):
        """Import signals when app is ready"""
        import payment.models  