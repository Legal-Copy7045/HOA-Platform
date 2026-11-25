from django.conf import settings
from django.urls import path
from django.views.generic import TemplateView

from core.views import healthz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("", TemplateView.as_view(template_name="home.html"), name="home"),
]

if settings.ADMIN_ENABLED:
    from django.contrib import admin

    urlpatterns.append(path(settings.ADMIN_URL, admin.site.urls))
