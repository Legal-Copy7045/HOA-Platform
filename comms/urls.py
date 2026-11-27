from django.urls import path, re_path

from comms import bounce_views, papermail_views, pwa_views, rsvp_views, views

urlpatterns = [
    # Paper mail (D55): board and manager only.
    path("paper-mail/", papermail_views.paper_mail, name="paper_mail"),
    path("paper-mail/new/", papermail_views.paper_mail_new, name="paper_mail_new"),
    # Bounced email (D74): a bounced official notice's paper copy went another way.
    path(
        "paper-mail/bounced/<int:pk>/sent/",
        bounce_views.bounce_paper_done,
        name="bounce_paper_done",
    ),
    path(
        "board/people/<int:pk>/email-bounces/clear/",
        bounce_views.person_email_bounce_clear,
        name="person_email_bounce_clear",
    ),
    path("paper-mail/settings/", papermail_views.paper_mail_settings, name="paper_mail_settings"),
    path("paper-mail/settings/test.pdf", papermail_views.paper_mail_test, name="paper_mail_test"),
    path("paper-mail/<int:pk>/", papermail_views.paper_mail_detail, name="paper_mail_detail"),
    path(
        "paper-mail/<int:pk>/letters.pdf",
        papermail_views.paper_mail_letters,
        name="paper_mail_letters",
    ),
    path(
        "paper-mail/<int:pk>/envelopes.pdf",
        papermail_views.paper_mail_envelopes,
        name="paper_mail_envelopes",
    ),
    path(
        "paper-mail/<int:pk>/labels.pdf",
        papermail_views.paper_mail_labels,
        name="paper_mail_labels",
    ),
    path("paper-mail/<int:pk>/mailed/", papermail_views.paper_mail_mark, name="paper_mail_mark"),
    path(
        "paper-mail/<int:pk>/checked/",
        papermail_views.paper_mail_piece_checked,
        name="paper_mail_piece_checked",
    ),
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
    # Event RSVPs and volunteer sign-ups (comms.rsvp)
    path("calendar/<int:pk>/", rsvp_views.event_detail, name="event_detail"),
    path("calendar/<int:pk>/event.ics", views.event_ics, name="event_ics"),
    path("calendar/<int:pk>/rsvp/", rsvp_views.event_rsvp, name="event_rsvp"),
    path(
        "calendar/<int:pk>/rsvp-settings/",
        rsvp_views.event_rsvp_settings,
        name="event_rsvp_settings",
    ),
    path("calendar/<int:pk>/slots/add/", rsvp_views.event_slot_add, name="event_slot_add"),
    re_path(
        r"^calendar/slots/(?P<pk>[0-9]+)/(?P<action>join|leave|remove)/$",
        rsvp_views.event_slot_action,
        name="event_slot_action",
    ),
    path("calendar/<int:pk>/rsvps.csv", rsvp_views.event_rsvp_export, name="event_rsvp_export"),
    re_path(
        r"^threads/(?P<kind>request|arc|violation|work_order|committee_topic)/(?P<pk>[0-9]+)/post/$",
        views.thread_post,
        name="thread_post",
    ),
]
