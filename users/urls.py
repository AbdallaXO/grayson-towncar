# urls.py - Updated URL patterns for multiple agency heads
from django.urls import path, reverse_lazy
from . import views, partner_views
from users.emails import send_reservation_confirmation
from django.contrib.auth import views as auth_views

urlpatterns = [
    path("partner/draft/", partner_views.save_draft, name="partner_draft"),
    path("partner/setup/", partner_views.setup, name="partner_setup"),
    path("partner/continue/<str:token>/", partner_views.continue_inquiry, name="partner_continue"),
    path("partner/verify/<str:token>/", partner_views.verify, name="partner_verify"),
    path("partner/resend/", partner_views.resend, name="partner_resend"),
    path("partner/agencies/<int:pk>/", partner_views.agency_workspace, name="partner_agency"),
    path("partner/staff/", partner_views.staff_workspace, name="partner_staff"),
    path("partner/payouts/", partner_views.payout_board, name="partner_payouts"),
    path("", views.partner, name="partner"),
    path("login/", views.loginUser, name="login"),
    path("logout/", views.logoutUser, name="logout"),
    path("register/", views.registerUser, name="register"),
    path("thank-you/", views.thankYou, name="thankyou"),
    path("contact-grayson-towncar/", views.contact, name="contact"),
    path(
        "newsletter/subscribe/", views.newsletter_subscribe, name="newsletter_subscribe"
    ),
    path("agent/register/", partner_views.register, name="register_agent"),
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
    path(
        "agent/reservation/<uuid:uuid>/send-email/",
        views.send_custom_confirmation_email,
        name="send_custom_confirmation_email",
    ),
    path(
        "agent/reservation/<uuid:uuid>/mark-personal/",
        views.agent_mark_personal_trip,
        name="agent_mark_personal_trip",
    ),
    path("agent/profile/", partner_views.setup, name="agent_profile"),
    path("agent-login/", views.agent_login, name="agent_login"),
    # Agency management URLs - Updated for multiple heads
    path(
        "agency/dashboard/",
        partner_views.agency_workspace,
        name="agency_dashboard",
    ),
    path("agency/<int:pk>/", partner_views.agency_workspace, name="agency_detail"),
    path(
        "agency/<int:pk>/agents/",
        partner_views.agency_workspace,
        name="agency_agents_list",
    ),
    path("agent/<int:pk>/", partner_views.agent_detail, name="agent_detail"),
    path(
        "commission-payout/<int:pk>/",
        views.commission_payout_detail,
        name="commission_payout_detail",
    ),
    path(
        "agency_commission_history/<agency_id>/",
        partner_views.agency_workspace,
        name="agency_commission_history",
    ),
    path(
        "agency_commission_payout/<int:payout_id>/",
        views.agency_commission_payout_detail,
        name="agency_commission_payout_detail",
    ),
    path(
        "commission-payout/<int:pk>/send-statement/",
        views.send_agent_commission_statement_email,
        name="send_agent_commission_statement",
    ),
    path(
        "agency_commission_payout/<int:payout_id>/send-statement/",
        views.send_agency_commission_statement_email,
        name="send_agency_commission_statement",
    ),
    # New agency management URLs
    path(
        "agency/profile/",
        partner_views.agency_workspace,
        name="agency_profile",
    ),
    path(
        "agency/guide/",
        partner_views.agency_workspace,
        name="agency_guide",
    ),
    path(
        "agency/update-payment/",
        partner_views.agency_workspace,
        name="update_agency_payment",
    ),
    # Admin commission report
    path(
        "admin/commission-report/",
        views.admin_commission_report,
        name="admin_commission_report",
    ),
    path(
        "admin/commission-report/export-csv/",
        views.admin_commission_export_csv,
        name="admin_commission_export_csv",
    ),
    path(
        "admin/commission-report/email/",
        views.admin_commission_email_report,
        name="admin_commission_email_report",
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
    path("agencies/<int:pk>/", partner_views.agency_workspace, name="agency_detail"),
    path(
        "agencies/<int:pk>/edit/",
        views.AgencyUpdateView.as_view(),
        name="agency_update",
    ),
    path(
        "password-reset/",
        views.PartnerPasswordResetView.as_view(),
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
