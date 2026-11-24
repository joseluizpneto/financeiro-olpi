from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from .. import services as sv
from ..models import Conta, Veiculo
from ..utils import hoje, parse_mes
from ._comum import ZERO, _escopo, _nav_mes


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    sv.gerar_recorrencias(forcar=False)
    ini = parse_mes(request.GET.get("mes"))
    escopo = _escopo(request)

    contas_qs = sv.filtrar_escopo_contas(Conta.objects.filter(ativo=True).select_related("titular"), escopo)
    contas = sv.saldos_contas(contas_qs)

    qs = sv.filtrar_escopo_lancamentos(sv.lancamentos_do_mes(ini), escopo)
    receitas = sv.soma(qs.filter(tipo="R"))
    despesas = sv.despesas_liquidas(qs)
    categorias = list(sv.despesas_por_categoria(qs))

    pendentes, faturas = sv.contas_a_vencer(hoje() + timedelta(days=10))
    veiculos = list(Veiculo.objects.filter(ativo=True))
    alertas_veiculos = []
    for v in veiculos:
        for a in sv.alertas_manutencao(v):
            alertas_veiculos.append({"veiculo": v, **a})

    veiculo_principal = veiculos[0] if veiculos else None
    alertas_principal = [a for a in alertas_veiculos if a["veiculo"].pk == veiculo_principal.pk] if veiculo_principal else []

    return render(
        request,
        "financeiro/dashboard.html",
        {
            "nav": _nav_mes(request, ini),
            "escopo": escopo,
            "saldo_total": sum((c.saldo_atual for c in contas), ZERO),
            "titulares": sv.saldos_por_titular(contas),
            "receitas": receitas,
            "despesas": despesas,
            "resultado": receitas - despesas,
            "pendentes_mes": sv.soma(qs.filter(pago=False, tipo="D")),
            "categorias": categorias,
            "grafico": {"labels": [c["grupo"] for c in categorias], "valores": [float(c["t"]) for c in categorias]},
            "pendentes": pendentes,
            "faturas": faturas,
            "cartoes": sv.resumo_cartoes(),
            "orcamento": sv.orcamento_mes(ini)[:6],
            "veiculo": veiculo_principal,
            "veiculos": veiculos,
            "alertas": alertas_principal,
            "alertas_veiculos": alertas_veiculos,
            "metas": sv.resumo_metas()[:4],
            "fluxo_resumo": sv.fluxo_caixa_projetado(dias=30, escopo=escopo),
            "hoje": hoje(),
            "sem_cadastro": not Conta.objects.exists(),
        },
    )


@login_required
def fluxo_caixa(request: HttpRequest) -> HttpResponse:
    try:
        dias = int(request.GET.get("dias") or 60)
    except ValueError:
        dias = 60
    if dias not in (30, 60, 90):
        dias = 60
    escopo = _escopo(request)
    conta_str = request.GET.get("conta", "")
    conta_id = int(conta_str) if conta_str.isdigit() else None
    dados = sv.fluxo_caixa_projetado(dias=dias, conta_id=conta_id, escopo=escopo)
    return render(
        request,
        "financeiro/fluxo_caixa.html",
        {
            "dados": dados,
            "dias": dias,
            "escopo": escopo,
            "conta_id": conta_id,
            "contas": Conta.objects.filter(ativo=True).select_related("titular"),
            "grafico": {
                "labels": [p["data"].strftime("%d/%m") for p in dados["serie"]],
                "saldos": [float(p["saldo"]) for p in dados["serie"]],
            },
        },
    )
