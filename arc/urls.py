from django.urls import path, re_path

from arc import signoff_views as sv
from arc import views

urlpatterns = [
    path("board/arc-types/", views.project_types, name="arc_project_types"),
    path("arc/", views.arc_list, name="arc_list"),
    path("arc/<int:pk>/", views.arc_detail, name="arc_detail"),
    re_path(r"^arc/(?P<pk>[0-9]+)/do/(?P<action>[a-z_]+)/$", views.arc_action, name="arc_action"),
    re_path(
        r"^arc/(?P<pk>[0-9]+)/decide/(?P<kind>decision|appeal)/$",
        views.arc_decide,
        name="arc_decide",
    ),
    path("arc/<int:pk>/position/", views.arc_position, name="arc_position"),
    path("arc/<int:pk>/files/", views.arc_attach, name="arc_attach"),
    path("arc/<int:pk>/files/<int:file_id>/", views.arc_file, name="arc_file"),
    path("arc/<int:pk>/letters/<int:decision_id>.pdf", views.arc_letter, name="arc_letter"),
    path("arc/<int:pk>/affected/", views.arc_affected, name="arc_affected"),
    re_path(
        r"^arc/(?P<pk>[0-9]+)/signoffs/(?P<action>[a-z_]+)/$",
        sv.arc_signoff_action,
        name="arc_signoff_action",
    ),
    path("arc/<int:pk>/signoff-form.pdf", sv.arc_signoff_form, name="arc_signoff_form"),
    path("board/arc-neighbors/", sv.neighbors_page, name="arc_neighbors"),
    path("signoffs/", sv.signoffs_mine, name="signoffs_mine"),
    path("signoffs/<int:pk>/", sv.signoff_detail, name="signoff_detail"),
    path("signoffs/<int:pk>/files/<int:file_id>/", sv.signoff_file, name="signoff_file"),
    re_path(r"^s/(?P<token>[A-Za-z0-9_-]{20,100})/$", sv.signoff_link, name="signoff_link"),
    re_path(
        r"^s/(?P<token>[A-Za-z0-9_-]{20,100})/files/(?P<file_id>[0-9]+)/$",
        sv.signoff_link_file,
        name="signoff_link_file",
    ),
]
