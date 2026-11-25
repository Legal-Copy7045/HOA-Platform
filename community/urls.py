from django.urls import path, re_path

from community import account_views, branding_views, views

urlpatterns = [
    path("board/branding/", branding_views.branding_settings, name="branding_settings"),
    path(
        "branding/login-background/<int:pk>.jpg",
        branding_views.login_background_image,
        name="login_background_image",
    ),
    path("my-lot/", views.my_lot, name="my_lot"),
    path("my-history/", account_views.my_history, name="my_history"),
    path("account/contacts/add/", account_views.contact_method_add, name="contact_method_add"),
    path(
        "account/contacts/<int:pk>/remove/",
        account_views.contact_method_remove,
        name="contact_method_remove",
    ),
    path(
        "board/accounts/<int:pk>/number/", account_views.account_number, name="lot_account_number"
    ),
    path(
        "board/accounts/<int:pk>/people/add/",
        account_views.account_contact_add,
        name="lot_account_contact_add",
    ),
    path(
        "board/account-people/<int:pk>/end/",
        account_views.account_contact_end,
        name="lot_account_contact_end",
    ),
    path("lots/", views.lot_list, name="lot_list"),
    path("lots/<int:pk>/", views.lot_detail, name="lot_detail"),
    path("lots/<int:pk>/transfer/", views.lot_transfer, name="lot_transfer"),
    path("board/", views.board_home, name="board_home"),
    path("board/community/", views.community_settings, name="community_settings"),
    path("board/community/rules/", views.community_rule_save, name="community_rule_save"),
    path("board/community/profile/", views.community_profile_save, name="community_profile_save"),
    path("board/community/areas/", views.common_area_save, name="common_area_save"),
    path("board/approvals/", views.approvals, name="approvals"),
    path("board/people/", views.people, name="people"),
    path("board/people/<int:pk>/", views.person_detail, name="person_detail"),
    path(
        "board/people/<int:pk>/ownerships/", views.person_add_ownership, name="person_add_ownership"
    ),
    path("board/people/<int:pk>/seats/", views.person_add_seat, name="person_add_seat"),
    path("board/people/<int:pk>/offices/", views.person_add_office, name="person_add_office"),
    path("board/people/<int:pk>/roles/", views.person_add_role, name="person_add_role"),
    path(
        "board/people/<int:pk>/nominate-sysadmin/",
        views.sysadmin_nominate,
        name="sysadmin_nominate",
    ),
    path("board/ownerships/<int:pk>/end/", views.ownership_end, name="ownership_end"),
    path("board/seats/<int:pk>/end/", views.seat_end, name="seat_end"),
    path("board/offices/<int:pk>/end/", views.office_end, name="office_end"),
    path("board/roles/<int:pk>/end/", views.role_end, name="role_end"),
    re_path(
        r"^board/nominations/(?P<pk>[0-9]+)/(?P<decision>approve|decline)/$",
        views.nomination_decide,
        name="nomination_decide",
    ),
]
