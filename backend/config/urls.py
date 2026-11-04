from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from publishing.local_login_views import LocalThrottledLoginView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),
    path("login/", LocalThrottledLoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("publishing.urls")),
]
