from django.conf import settings
from django.http import Http404
from django.urls import include, path

from audit.views import sysadmin_activity
from community.profile_views import directory, my_profile
from core import preview
from core.views import export_my_data, healthz, home, system_status


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
    # Invite-only, and no self-service change of email address (D5). These
    # come before allauth's own URLs so they win.
    path("accounts/signup/", not_available),
    path("accounts/email/", not_available),
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
    path("", include("reports.urls")),
]

if settings.ADMIN_ENABLED:
    from django.contrib import admin

    urlpatterns.append(path(settings.ADMIN_URL, admin.site.urls))
