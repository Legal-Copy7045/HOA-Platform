from django.conf import settings
from django.http import Http404
from django.urls import include, path, re_path

from accounts import mfa_views
from audit.views import sysadmin_activity
from community.profile_views import directory, my_profile
from core import hub_views, preview
from core.followups import set_follow_up
from core.my_items_views import my_items_page
from core.search_views import search_page
from core.views import export_my_data, feature_settings, healthz, home, system_status


def not_available(request, *args, **kwargs):
    raise Http404


urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("", home, name="home"),
    path("ops/status/", system_status, name="system_status"),
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
    path("community-info/", hub_views.community_info, name="community_info"),
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
