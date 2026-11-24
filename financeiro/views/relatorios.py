from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from .. import services as sv
from ..models import CentroCusto
from ..utils import parse_mes
from ._comum import _ano, _escopo, _nav_mes


@login_required
def empresa(request: HttpRequest) -> HttpResponse:
    centros = list(CentroCusto.objects.filter(empresa=True, ativo=True))
    if not centros:
        return render(request, "financeiro/empresa.html", {"centro": None})
    sel = request.GET.get("centro", "")
    centro = next((c for c in centros if str(c.pk) == sel), centros[0])
    ini = parse_mes(request.GET.get("mes"))
    dados = sv.resumo_empresa(centro, ini)
    meses = dados["meses"]
    return render(
        request,
        "financeiro/empresa.html",
        {
            "centros": centros,
            "centro": centro,
            "nav": _nav_mes(request, ini),
            "ini": ini,
            **dados,
            "grafico": {
                "labels": [m["mes"].strftime("%m/%Y") for m in meses],
                "entradas": [float(m["entradas"]) for m in meses],
                "saidas": [float(m["saidas"]) for m in meses],
                "impostos": [float(m["impostos"]) for m in meses],
            },
        },
    )


@login_required
def relatorios(request: HttpRequest) -> HttpResponse:
    ano = _ano(request)
    escopo = _escopo(request)
    dados = sv.resumo_anual(ano, escopo=escopo)
    meses = dados["meses"]
    return render(
        request,
        "financeiro/relatorios.html",
        {
            "ano": ano,
            "escopo": escopo,
            **dados,
            "grafico": {
                "labels": [m["mes"].strftime("%m") for m in meses],
                "receitas": [float(m["receitas"]) for m in meses],
                "despesas": [float(m["despesas"]) for m in meses],
            },
        },
    )


@login_required
def rateio(request: HttpRequest) -> HttpResponse:
    ini = parse_mes(request.GET.get("mes"))
    modo = request.GET.get("modo", "igual")
    if modo not in ("igual", "renda"):
        modo = "igual"
    incluir_casa = request.GET.get("casa", "1") != "0"
    dados = sv.rateio_mensal(ini, modo=modo, incluir_casa=incluir_casa)
    return render(
        request,
        "financeiro/rateio.html",
        {
            "nav": _nav_mes(request, ini),
            **dados,
        },
    )
