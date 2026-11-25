from django.urls import path, re_path

from comms import pwa_views, views

urlpatterns = [
    path("announcements/", views.announcements, name="announcements"),
    path("announcements/<int:pk>/", views.announcement_detail, name="announcement_detail"),
    path("calendar/", views.calendar, name="calendar"),
    path("faq/", views.faq, name="faq"),
    # Installable app (served from the site root so the worker's scope is "/").
    path("manifest.webmanifest", pwa_views.manifest, name="pwa_manifest"),
    path("sw.js", pwa_views.service_worker, name="pwa_service_worker"),
    path("account/push/subscribe/", pwa_views.push_subscribe, name="push_subscribe"),
    path("account/push/unsubscribe/", pwa_views.push_unsubscribe, name="push_unsubscribe"),
    path("account/texts/start/", pwa_views.sms_start, name="sms_start"),
    path("account/texts/verify/", pwa_views.sms_verify, name="sms_verify"),
    path("account/texts/stop/", pwa_views.sms_stop, name="sms_stop"),
    path("board/urgent-text/", pwa_views.urgent_notice, name="urgent_notice"),
    path("calendar/<int:pk>/cancel/", views.event_cancel, name="event_cancel"),
    re_path(
        r"^threads/(?P<kind>request|arc|violation|work_order|committee_topic)/(?P<pk>[0-9]+)/post/$",
        views.thread_post,
        name="thread_post",
    ),
]
