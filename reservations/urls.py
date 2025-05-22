from django.urls import path
from . import views


urlpatterns = [
    path("", views.index, name="home"),
    path("orlando-transportation-faqs/", views.faqs, name="faqs"),
    path("about-grayson-towncar-services/", views.about_us, name="about-us"),
    path("book-orlando-transportation/<pk>", views.reservation_form, name="reserve"),
    path("tos", views.tos, name="tos"),
    path("test-quote/", views.test_quote, name="test-quote"),
        path('quote-form-handler/', views.quote_form_handler, name='quote_form_handler'),

]
