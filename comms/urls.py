from django.urls import path, re_path

from comms import views

urlpatterns = [
    path("announcements/", views.announcements, name="announcements"),
    path("announcements/<int:pk>/", views.announcement_detail, name="announcement_detail"),
    path("calendar/", views.calendar, name="calendar"),
    path("faq/", views.faq, name="faq"),
    path("calendar/<int:pk>/cancel/", views.event_cancel, name="event_cancel"),
    re_path(
        r"^threads/(?P<kind>request|arc|violation|work_order|committee_topic)/(?P<pk>[0-9]+)/post/$",
        views.thread_post,
        name="thread_post",
    ),
]
