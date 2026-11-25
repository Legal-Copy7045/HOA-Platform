from django.urls import path, re_path

from documents import views

urlpatterns = [
    path("documents/", views.library, name="documents"),
    path("documents/<int:pk>/", views.detail, name="document_detail"),
    path("documents/<int:pk>/download/", views.download, name="document_download"),
    path(
        "documents/<int:pk>/v/<int:version>/download/",
        views.download_version,
        name="document_version_download",
    ),
    path("documents/<int:pk>/versions/", views.add_version, name="document_add_version"),
    re_path(
        r"^documents/(?P<pk>[0-9]+)/(?P<action>archive|restore)/$",
        views.archive,
        name="document_archive",
    ),
]
