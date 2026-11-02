from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("workspace/", views.workspace, name="workspace"),
    path("journals/<slug:slug>/new/", views.submission_create, name="submission_create"),
    path("submissions/<uuid:pk>/", views.submission_detail, name="submission_detail"),
    path("submissions/<uuid:pk>/submit/", views.submission_submit, name="submission_submit"),
    path("submissions/<uuid:pk>/file/", views.submission_file, name="submission_file"),
    path("submissions/<uuid:pk>/versions/<int:number>/file/", views.submission_version_file, name="submission_version_file"),
    path("submissions/<uuid:pk>/versions/<int:number>/review-copy/", views.submission_review_copy_file, name="submission_review_copy_file"),
    path("submissions/<uuid:pk>/review-copy-check/", views.review_copy_screen, name="review_copy_screen"),
    path("submissions/<uuid:pk>/revision/", views.submission_revision, name="submission_revision"),
    path("submissions/<uuid:pk>/decision/", views.editorial_decision, name="editorial_decision"),
    path("submissions/<uuid:pk>/invite/", views.review_invite, name="review_invite"),
    path("reviews/<uuid:pk>/", views.review_detail, name="review_detail"),
    path("reviews/<uuid:pk>/file/", views.review_file, name="review_file"),
    path("reviews/<uuid:pk>/respond/", views.review_respond, name="review_respond"),
    path("reviews/<uuid:pk>/report/", views.review_report, name="review_report"),
]
