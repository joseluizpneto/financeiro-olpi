from __future__ import annotations

from django.http import HttpRequest

from .services import tem_empresa_ativa


def contexto_global(request: HttpRequest) -> dict[str, bool]:
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {"tem_empresa": False}
    return {"tem_empresa": tem_empresa_ativa(request)}
