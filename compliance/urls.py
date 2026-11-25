from django.urls import path, re_path

from compliance import views

urlpatterns = [
    path("violations/", views.violations, name="violations"),
    path("violations/<int:pk>/", views.violation_detail, name="violation_detail"),
    re_path(
        r"^violations/(?P<pk>[0-9]+)/do/(?P<action>[a-z_]+)/$",
        views.violation_action,
        name="violation_action",
    ),
    path("violations/<int:pk>/respond/", views.violation_respond, name="violation_respond"),
    path("violations/<int:pk>/photos/", views.violation_photo, name="violation_photo"),
    path("violations/<int:pk>/photos/<int:file_id>/", views.violation_file, name="violation_file"),
    path("violations/<int:pk>/notice.pdf", views.violation_letter, name="violation_letter"),
]
