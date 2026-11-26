from django.urls import path, re_path

from accounts import access_review, board_views, recovery_views, views

urlpatterns = [
    path("invite/accept/", views.invite_accept, name="invite_accept"),
    path("board/onboarding/", views.onboarding, name="onboarding"),
    path("invite/<str:token>/", views.invite_landing, name="invite_landing"),
    path("account/enroll-code/", views.enroll_with_code, name="enroll_with_code"),
    path("account/setup-expired/", views.account_setup_expired, name="account_setup_expired"),
    path("account/setup/done/", views.account_setup_done, name="account_setup_done"),
    path("sign-in-help/", views.signin_help, name="signin_help"),
    # Self-service account recovery (D58): sign-in steps, then signed-in pages.
    path("sign-in-help/recover/", recovery_views.recovery_request, name="recovery_request"),
    path(
        "sign-in-help/recover/continue/",
        recovery_views.recovery_continue,
        name="recovery_continue",
    ),
    path("sign-in-help/recover/<str:token>/", recovery_views.recovery_link, name="recovery_link"),
    path(
        "account/recovery/",
        recovery_views.account_recovery_status,
        name="account_recovery_status",
    ),
    path("board/recoveries/<int:pk>/", recovery_views.recovery_board, name="recovery_board"),
    path("board/invitations/", board_views.invitations, name="invitations"),
    path("board/people/<int:pk>/invite/", board_views.invite_person, name="invite_person"),
    re_path(
        r"^board/invitations/(?P<pk>[0-9]+)/(?P<action>resend|revoke)/$",
        board_views.invitation_action,
        name="invitation_action",
    ),
    path("board/people/<int:pk>/reset/", board_views.reset_start, name="reset_start"),
    re_path(
        r"^board/resets/(?P<pk>[0-9]+)/(?P<decision>approve|cancel)/$",
        board_views.reset_decide,
        name="reset_decide",
    ),
    path("board/access-review/", access_review.access_review, name="access_review"),
    path(
        "board/access-review/record/",
        access_review.access_review_record,
        name="access_review_record",
    ),
]
