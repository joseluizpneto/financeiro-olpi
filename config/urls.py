from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path

from financeiro.security import LoginProtegidoView

admin.site.site_header = "Controle Financeiro · Administração"
admin.site.site_title = "Controle Financeiro"


def health(_request):
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
    path("auth/login/", LoginProtegidoView.as_view(), name="login"),
    path("auth/", include("django.contrib.auth.urls")),
    path("", include("financeiro.urls")),
]
