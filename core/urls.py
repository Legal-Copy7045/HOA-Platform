from django.conf import settings
from django.http import Http404
from django.urls import include, path

from audit.views import sysadmin_activity
from core.views import healthz, home, system_status


def not_available(request, *args, **kwargs):
    raise Http404


urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("", home, name="home"),
    path("ops/status/", system_status, name="system_status"),
    path("board/sysadmin-activity/", sysadmin_activity, name="sysadmin_activity"),
    # Invite-only, and no self-service change of email address (D5). These
    # come before allauth's own URLs so they win.
    path("accounts/signup/", not_available),
    path("accounts/email/", not_available),
    path("accounts/", include("allauth.urls")),
    path("", include("accounts.urls")),
    path("", include("community.urls")),
]

if settings.ADMIN_ENABLED:
    from django.contrib import admin

    urlpatterns.append(path(settings.ADMIN_URL, admin.site.urls))
