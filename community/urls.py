from django.urls import path, re_path

from community import views

urlpatterns = [
    path("my-lot/", views.my_lot, name="my_lot"),
    path("lots/", views.lot_list, name="lot_list"),
    path("lots/<int:pk>/", views.lot_detail, name="lot_detail"),
    path("lots/<int:pk>/transfer/", views.lot_transfer, name="lot_transfer"),
    path("board/", views.board_home, name="board_home"),
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
