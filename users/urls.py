# urls.py - Updated URL patterns with process_agency_commissions
from django.urls import path
from . import views
from users.emails import send_reservation_confirmation

urlpatterns = [
    path("", views.partner, name="partner"),
    path("login/", views.loginUser, name="login"),
    path("logout/", views.logoutUser, name="logout"),
    path("register/", views.registerUser, name="register"),
    path("thank-you/", views.thankYou, name="thankyou"),
    path("contact-grayson-towncar/", views.contact, name="contact"),
    path(
        "newsletter/subscribe/", views.newsletter_subscribe, name="newsletter_subscribe"
    ),
    path("agent/register/", views.register_agent, name="register_agent"),
    path("agent/dashboard/", views.agent_dashboard, name="agent_dashboard"),
    path(
        "agent/commissions/",
        views.agent_commission_history,
        name="agent_commission_history",
    ),
    path(
        "agent/reservation/<uuid:uuid>/",
        views.agent_reservation_detail,
        name="agent_reservation_detail",
    ),
    path("agent/profile/", views.agent_profile, name="agent_profile"),
    path("agent-login/", views.agent_login, name="agent_login"),
    
    # Agency management URLs
    path('agency/dashboard/', views.AgencyDashboardView.as_view(), name='agency_dashboard'),
    path('agency/<int:pk>/agents/', views.AgencyAgentsListView.as_view(), name='agency_agents_list'),
    path('agent/<int:pk>/', views.AgentDetailView.as_view(), name='agent_detail'),
    
    # AJAX endpoints for updating agent stats and processing payments
    path('agent/<int:pk>/update-stats/', views.update_agent_commission_stats, name='update_agent_commission_stats'),
    path('agent/<int:pk>/process-commission/', views.process_agent_commission, name='process_agent_commission'),
    path('agency/<int:pk>/process-commissions/', views.process_agency_commissions, name='process_agency_commissions'),
    path('commission-payout/<int:pk>/', views.commission_payout_detail, name='commission_payout_detail'),
]