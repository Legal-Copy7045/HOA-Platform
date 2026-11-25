from django.urls import path, re_path

from governance import views

urlpatterns = [
    path("meetings/", views.meetings, name="meetings"),
    path("meetings/<int:pk>/", views.meeting_detail, name="meeting_detail"),
    re_path(
        r"^meetings/(?P<pk>[0-9]+)/(?P<action>notice|held|cancelled|minutes)/$",
        views.meeting_action,
        name="meeting_action",
    ),
    path("resolutions/", views.resolutions, name="resolutions"),
    path("records-requests/", views.records_requests, name="records_requests"),
    path("records-requests/<int:pk>/", views.records_request_detail, name="records_request_detail"),
    re_path(
        r"^records-requests/(?P<pk>[0-9]+)/(?P<action>[a-z_]+)/$",
        views.records_request_action,
        name="records_request_action",
    ),
    path("votes/", views.votes, name="votes"),
    path("votes/<int:pk>/", views.vote_detail, name="vote_detail"),
    re_path(
        r"^votes/(?P<pk>[0-9]+)/(?P<action>cast|open|close)/$",
        views.vote_action,
        name="vote_action",
    ),
]
