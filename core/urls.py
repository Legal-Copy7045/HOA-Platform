from django.conf import settings
from django.http import Http404
from django.urls import include, path, re_path

from accounts import mfa_views, password_reset, recovery_codes
from audit.views import sysadmin_activity
from community.profile_views import directory, my_profile
from core import demo_mode, hub_views, preview
from core.followups import set_follow_up
from core.keys_views import keys_change, keys_connections, keys_service
from core.my_items_views import my_items_page
from core.search_views import search_page
from core.server_task_views import demo_data, server_tasks, system_restart
from core.views import (
    export_my_data,
    feature_settings,
    healthz,
    home,
    ops_timeline,
    system_status,
)


def not_available(request, *args, **kwargs):
    raise Http404


urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("", home, name="home"),
    path("ops/status/", system_status, name="system_status"),
    path("ops/status/restart/", system_restart, name="system_restart"),
    # Keys and connections (D93): sysadmins; Stripe and Teller changes also the board.
    path("ops/status/keys/", keys_connections, name="keys_connections"),
    path("ops/status/keys/change/<int:pk>/", keys_change, name="keys_change"),
    path("ops/status/keys/<slug:service>/", keys_service, name="keys_service"),
    path("ops/tasks/", server_tasks, name="server_tasks"),
    path("ops/timeline/", ops_timeline, name="ops_timeline"),
    path("account/profile/", my_profile, name="my_profile"),
    path("account/export/", export_my_data, name="export_my_data"),
    path("directory/", directory, name="directory"),
    path("board/homeowner-view/", preview.start, name="owner_preview_start"),
    path("homeowner-view/exit/", preview.stop, name="owner_preview_stop"),
    path("board/sysadmin-activity/", sysadmin_activity, name="sysadmin_activity"),
    path("my-items/", my_items_page, name="my_items"),
    path("search/", search_page, name="search"),
    path("board/features/", feature_settings, name="feature_settings"),
    path("board/setup/", hub_views.settings_hub, name="settings_hub"),
    path("ops/demo-data/", demo_data, name="demo_data"),
    path("demo/switch/", demo_mode.switch, name="demo_switch"),
    path("demo/switch-back/", demo_mode.switch_back, name="demo_switch_back"),
    path("community-info/", hub_views.community_info, name="community_info"),
    path("finance/more/", hub_views.finance_more, name="finance_more"),
    re_path(
        r"^follow-up/(?P<kind>violation|collection|work_order)/(?P<pk>[0-9]+)/$",
        set_follow_up,
        name="follow_up_set",
    ),
    # Invite-only, and no self-service change of email address (D5). These
    # come before allauth's own URLs so they win.
    path("accounts/signup/", not_available),
    path("accounts/email/", not_available),
    # The last passkey or app cannot be removed (SEC-004).
    path("accounts/2fa/webauthn/", mfa_views.list_passkeys, name="mfa_list_webauthn"),
    path(
        "accounts/2fa/webauthn/keys/<int:pk>/remove/",
        mfa_views.remove_passkey,
        name="mfa_remove_webauthn",
    ),
    # D58: the reset link still needs the passkey, app or a recovery code.
    re_path(
        r"^accounts/password/reset/key/(?P<uidb36>[0-9A-Za-z]+)-(?P<key>.+)/$",
        password_reset.password_reset_from_key,
        name="account_reset_password_from_key",
    ),
    # D58: recovery codes are shown once, then printed or downloaded from there.
    path(
        "accounts/2fa/recovery-codes/",
        recovery_codes.view_codes,
        name="mfa_view_recovery_codes",
    ),
    path(
        "accounts/2fa/recovery-codes/download/",
        recovery_codes.download_codes,
        name="mfa_download_recovery_codes",
    ),
    path(
        "accounts/2fa/recovery-codes/print/",
        recovery_codes.print_codes,
        name="mfa_print_recovery_codes",
    ),
    path("accounts/", include("allauth.urls")),
    path("", include("accounts.urls")),
    path("", include("community.urls")),
    path("", include("comms.urls")),
    path("", include("operations.urls")),
    path("", include("documents.urls")),
    path("", include("governance.urls")),
    path("", include("arc.urls")),
    path("", include("compliance.urls")),
    path("", include("ledger.urls")),
    path("", include("payments.urls")),
    path("", include("reports.urls")),
]

if settings.ADMIN_ENABLED:
    from django.contrib import admin

    urlpatterns.append(path(settings.ADMIN_URL, admin.site.urls))
