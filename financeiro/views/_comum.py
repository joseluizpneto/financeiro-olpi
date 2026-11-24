from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.http import url_has_allowed_host_and_scheme

from ..services import tem_empresa_ativa
from ..utils import ano_valido, hoje, mes_str, somar_meses

ZERO = Decimal("0.00")


def _voltar(request: HttpRequest, padrao: str) -> str:
    destino = request.POST.get("next") or request.GET.get("next")
    if destino and url_has_allowed_host_and_scheme(
        destino, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return destino
    return padrao


def _nav_mes(request: HttpRequest, ini: date) -> dict[str, Any]:
    def qs(mes: date) -> str:
        g = request.GET.copy()
        g["mes"] = mes_str(mes)
        g.pop("page", None)
        return g.urlencode()

    return {
        "mes": ini,
        "atual": mes_str(ini),
        "qs_anterior": qs(somar_meses(ini, -1, dia=1)),
        "qs_proximo": qs(somar_meses(ini, 1, dia=1)),
        "eh_mes_atual": ini == hoje().replace(day=1),
        "qs_hoje": qs(hoje()),
    }


def _ano(request: HttpRequest) -> int:
    try:
        ano = int(request.GET.get("ano") or hoje().year)
        return ano if ano_valido(ano) else hoje().year
    except ValueError:
        return hoje().year


def _escopo(request: HttpRequest) -> str:
    if not tem_empresa_ativa(request):
        return "todos"
    val = (request.GET.get("escopo") or "todos").strip().lower()
    return val if val in ("todos", "pessoal", "empresa") else "todos"


def _render_form(request: HttpRequest, form, titulo: str, voltar: str, **extra: Any) -> HttpResponse:
    ctx = {"form": form, "titulo": titulo, "voltar": voltar}
    ctx.update(extra)
    return render(request, "financeiro/form.html", ctx)
