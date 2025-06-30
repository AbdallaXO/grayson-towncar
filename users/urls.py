# urls.py - Updated URL patterns for multiple agency heads
from django.urls import path, reverse_lazy
from . import views
from users.emails import send_reservation_confirmation
from django.contrib.auth import views as auth_views

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
    # Agency management URLs - Updated for multiple heads
    path(
        "agency/dashboard/",
        views.AgencyDashboardView.as_view(),
        name="agency_dashboard",
    ),
    path("agency/<int:pk>/", views.AgencyDetailView.as_view(), name="agency_detail"),
    path(
        "agency/<int:pk>/agents/",
        views.AgencyAgentsListView.as_view(),
        name="agency_agents_list",
    ),
    path("agent/<int:pk>/", views.AgentDetailView.as_view(), name="agent_detail"),
    path(
        "commission-payout/<int:pk>/",
        views.commission_payout_detail,
        name="commission_payout_detail",
    ),
    path(
        "agency_commission_history/<agency_id>/",
        views.agency_commission_history,
        name="agency_commission_history",
    ),
    path(
        "agency_commission_payout/<int:payout_id>/",
        views.agency_commission_payout_detail,
        name="agency_commission_payout_detail",
    ),
    # New agency management URLs
    path(
        "agency/profile/",
        views.AgencyProfileView.as_view(),
        name="agency_profile",
    ),
    path(
        "agency/guide/",
        views.AgencyGuideView.as_view(),
        name="agency_guide",
    ),
    path(
        "agency/update-payment/",
        views.update_agency_payment,
        name="update_agency_payment",
    ),
    # Admin agency management URLs - Updated with admin prefix
    path("admin/agencies/", views.AgencyListView.as_view(), name="admin_agency_list"),
    path(
        "admin/agencies/<int:pk>/",
        views.AgencyDetailView.as_view(),
        name="admin_agency_detail",
    ),
    path(
        "admin/agencies/<int:pk>/edit/",
        views.AgencyUpdateView.as_view(),
        name="admin_agency_update",
    ),
    # Regular agency management URLs
    path("agencies/", views.AgencyListView.as_view(), name="agency_list"),
    path("agencies/<int:pk>/", views.AgencyDetailView.as_view(), name="agency_detail"),
    path(
        "agencies/<int:pk>/edit/",
        views.AgencyUpdateView.as_view(),
        name="agency_update",
    ),
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(template_name="users/password_reset.html"),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="users/password_reset_done.html"
        ),
        name="password_reset_done",
    ),
    path(
        "password-reset-confirm/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="users/password_reset_confirm.html",
            post_reset_login=True,
            success_url=reverse_lazy("agent_dashboard"),  # Redirect
        ),
        name="password_reset_confirm",
    ),
    path(
        "password-reset-complete/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="users/password_reset_complete.html"
        ),
        name="password_reset_complete",
    ),
]
