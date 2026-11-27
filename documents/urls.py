from django.urls import path, re_path

from documents import renewal_views, retention_views, views

urlpatterns = [
    path("documents/", views.library, name="documents"),
    path("documents/<int:pk>/", views.detail, name="document_detail"),
    path("documents/folders/new/", views.folder_create, name="folder_create"),
    path("documents/<int:pk>/move/", views.document_move, name="document_move"),
    path(
        "committees/<int:pk>/documents/upload/",
        views.committee_upload,
        name="committee_upload",
    ),
    path("documents/<int:pk>/download/", views.download, name="document_download"),
    path(
        "documents/<int:pk>/v/<int:version>/download/",
        views.download_version,
        name="document_version_download",
    ),
    path("documents/<int:pk>/versions/", views.add_version, name="document_add_version"),
    path("documents/<int:pk>/date/", views.set_date, name="document_set_date"),
    path("documents/<int:pk>/access/", views.access_history, name="document_access"),
    path("documents/<int:pk>/compare/", views.compare, name="document_compare"),
    # Renewals (D72)
    path("renewals/", renewal_views.renewals_list, name="renewals"),
    path("renewals/policies/new/", renewal_views.policy_new, name="renewal_policy_new"),
    path("renewals/policies/<int:pk>/", renewal_views.policy_detail, name="renewal_policy"),
    path("renewals/vendors/<int:pk>/", renewal_views.vendor_detail, name="renewal_vendor"),
    path("renewals/contracts/<int:pk>/", renewal_views.contract_detail, name="renewal_contract"),
    path(
        "renewals/contracts/<int:pk>/decide/",
        renewal_views.contract_decide,
        name="renewal_contract_decide",
    ),
    # Records retention (D72)
    path("records/retention/", retention_views.records_retention, name="records_retention"),
    path(
        "records/retention/propose/",
        retention_views.disposal_propose,
        name="records_disposal_propose",
    ),
    path(
        "records/retention/requests/<int:pk>/decide/",
        retention_views.disposal_decide,
        name="records_disposal_decide",
    ),
    path("records/retention/holds/new/", retention_views.hold_place, name="records_hold_place"),
    path(
        "records/retention/holds/<int:pk>/release/",
        retention_views.hold_release,
        name="records_hold_release",
    ),
    re_path(
        r"^documents/(?P<pk>[0-9]+)/(?P<action>archive|restore)/$",
        views.archive,
        name="document_archive",
    ),
]
