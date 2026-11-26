from django.urls import path, re_path

from governance import committee_views as cv
from governance import election_views as ev
from governance import packet_views as pv
from governance import survey_views as sv
from governance import views
from governance import voting_views as vv

urlpatterns = [
    path("committees/", cv.committee_list, name="committees"),
    path("committees/<int:pk>/", cv.committee_detail, name="committee_detail"),
    re_path(
        r"^committees/(?P<pk>[0-9]+)/do/(?P<action>appoint|retire|reactivate|topic|task|event)/$",
        cv.committee_action,
        name="committee_action",
    ),
    path(
        "committees/<int:pk>/tasks/<int:task_id>/close/",
        cv.committee_task_close,
        name="committee_task_close",
    ),
    path("committees/topics/<int:pk>/", cv.committee_topic_detail, name="committee_topic_detail"),
    path(
        "board/committee-members/<int:pk>/end/",
        cv.committee_member_end,
        name="committee_member_end",
    ),
    path("elections/", vv.elections, name="elections"),
    path("elections/<int:pk>/", ev.election_detail, name="election_detail"),
    re_path(
        r"^elections/(?P<pk>[0-9]+)/do/(?P<action>[a-z]+)/$",
        ev.election_action,
        name="election_action",
    ),
    path("meetings/<int:pk>/proxy/", ev.proxy_grant, name="proxy_grant"),
    path("proxies/<int:pk>/revoke/", ev.proxy_revoke, name="proxy_revoke"),
    path("board/consents/", ev.consents, name="consents"),
    path("board/consents/<int:pk>/", ev.consent_detail, name="consent_detail"),
    path("meetings/", views.meetings, name="meetings"),
    path("meetings/<int:pk>/", views.meeting_detail, name="meeting_detail"),
    re_path(
        r"^meetings/(?P<pk>[0-9]+)/(?P<action>notice|held|cancelled|minutes|approve_minutes)/$",
        views.meeting_action,
        name="meeting_action",
    ),
    path("meetings/<int:pk>/packet/", pv.meeting_packet, name="meeting_packet"),
    path(
        "meetings/<int:pk>/packets/<int:packet_id>/zip/",
        pv.meeting_packet_zip,
        name="meeting_packet_zip",
    ),
    path("resolutions/", views.resolutions, name="resolutions"),
    path("records-requests/", views.records_requests, name="records_requests"),
    path("records-requests/<int:pk>/", views.records_request_detail, name="records_request_detail"),
    re_path(
        r"^records-requests/(?P<pk>[0-9]+)/(?P<action>[a-z_]+)/$",
        views.records_request_action,
        name="records_request_action",
    ),
    path("voting/", vv.voting, name="voting"),
    path("surveys/", vv.surveys_list, name="surveys"),
    path("surveys/<int:pk>/", sv.survey_detail, name="survey_detail"),
    path("surveys/<int:pk>/results/", sv.survey_results, name="survey_results"),
    path("surveys/<int:pk>/export/", sv.survey_export, name="survey_export"),
    re_path(
        r"^surveys/(?P<pk>[0-9]+)/do/(?P<action>settings|question|open|close|share|unshare)/$",
        sv.survey_action,
        name="survey_action",
    ),
    re_path(
        r"^surveys/questions/(?P<pk>[0-9]+)/(?P<action>edit|up|down|remove)/$",
        sv.survey_question_action,
        name="survey_question_action",
    ),
    path("votes/", vv.votes, name="votes"),
    path("ballot/<str:token>/", views.ballot_link, name="ballot_link"),
    path("votes/<int:pk>/", views.vote_detail, name="vote_detail"),
    re_path(
        r"^votes/(?P<pk>[0-9]+)/(?P<action>cast|open|close)/$",
        views.vote_action,
        name="vote_action",
    ),
]
