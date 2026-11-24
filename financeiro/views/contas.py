from __future__ import annotations

from collections import OrderedDict
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import services as sv
from ..audit import registrar_auditoria
from ..forms import FechamentoForm
from ..models import Conta, Fatura, FechamentoMensal, Lancamento, RegistroAuditoria
from ..utils import fim_mes, hoje, mes_str, parse_mes
from ._comum import ZERO, _nav_mes


@login_required
def a_vencer(request: HttpRequest) -> HttpResponse:
    sv.gerar_recorrencias(forcar=False)
    h = hoje()
    pendentes, faturas = sv.contas_a_vencer(h + timedelta(days=31))
    itens = [{"data": l.vencimento, "lanc": l, "valor": l.valor, "receita": l.tipo == Lancamento.RECEITA} for l in pendentes]
    itens += [{"data": f.vencimento, "fatura": f, "valor": f.saldo, "receita": False} for f in faturas]
    itens.sort(key=lambda i: i["data"])
    grupos = OrderedDict([("Atrasados", []), ("Próximos 7 dias", []), ("Até 30 dias", [])])
    for i in itens:
        if i["data"] < h:
            grupos["Atrasados"].append(i)
        elif i["data"] <= h + timedelta(days=7):
            grupos["Próximos 7 dias"].append(i)
        else:
            grupos["Até 30 dias"].append(i)
    a_pagar = sum(
        (i["valor"] for i in itens if i.get("fatura") or i["lanc"].tipo == Lancamento.DESPESA),
        ZERO,
    )
    a_receber = sum(
        (i["valor"] for i in itens if i.get("lanc") and i["lanc"].tipo in (Lancamento.RECEITA, Lancamento.ESTORNO)),
        ZERO,
    )
    return render(
        request,
        "financeiro/a_vencer.html",
        {
            "grupos": grupos,
            "a_pagar": a_pagar,
            "a_receber": a_receber,
            "hoje": h,
        },
    )


@login_required
def conta_extrato(request: HttpRequest, pk: int) -> HttpResponse:
    conta = get_object_or_404(Conta.objects.select_related("titular"), pk=pk)
    ini = parse_mes(request.GET.get("mes"))
    fim = fim_mes(ini)
    saldo_anterior = sv.saldos_contas([conta], ate=ini - timedelta(days=1))[0].saldo_atual
    saldo_atual = sv.saldos_contas([conta], ate=hoje())[0].saldo_atual

    data_corte = max(ini, conta.data_saldo_inicial)
    movimentos = (
        Lancamento.objects.filter(
            Q(conta=conta) | Q(conta_destino=conta),
            pago=True,
            data_pagamento__gte=data_corte,
            data_pagamento__lte=fim,
        )
        .select_related("categoria__pai", "conta", "conta_destino", "fatura_paga__cartao", "criado_por")
        .order_by("data_pagamento", "id")
    )

    linhas = []
    saldo = saldo_anterior
    incluir_marco_inicial = ini <= conta.data_saldo_inicial <= fim
    marco_inserido = False
    if incluir_marco_inicial and conta.saldo_inicial == ZERO:
        marco_inserido = True

    for l in movimentos:
        if incluir_marco_inicial and not marco_inserido and l.data_pagamento >= conta.data_saldo_inicial:
            saldo += conta.saldo_inicial
            linhas.append({"marco_inicial": True, "data": conta.data_saldo_inicial, "valor": conta.saldo_inicial, "saldo": saldo})
            marco_inserido = True
        entrada = (l.tipo in (Lancamento.RECEITA, Lancamento.ESTORNO)) or (
            l.tipo == Lancamento.TRANSFERENCIA and l.conta_destino_id == conta.id
        )
        valor = l.valor if entrada else -l.valor
        saldo += valor
        linhas.append({"l": l, "valor": valor, "saldo": saldo})

    if incluir_marco_inicial and not marco_inserido:
        saldo += conta.saldo_inicial
        linhas.append({"marco_inicial": True, "data": conta.data_saldo_inicial, "valor": conta.saldo_inicial, "saldo": saldo})

    pendentes_qs = Lancamento.objects.filter(
        Q(conta=conta) | Q(conta_destino=conta), pago=False, vencimento__lte=fim
    ).select_related("conta", "conta_destino").order_by("vencimento")
    pendentes = []
    for l in pendentes_qs:
        l.entrada_nesta_conta = (l.tipo in (Lancamento.RECEITA, Lancamento.ESTORNO)) or (
            l.tipo == Lancamento.TRANSFERENCIA and l.conta_destino_id == conta.id
        )
        pendentes.append(l)

    return render(
        request,
        "financeiro/conta_extrato.html",
        {
            "conta": conta,
            "nav": _nav_mes(request, ini),
            "linhas": linhas,
            "saldo_anterior": saldo_anterior,
            "saldo_final": saldo,
            "saldo_atual": saldo_atual,
            "pendentes": pendentes,
            "hoje": hoje(),
        },
    )


@login_required
@require_POST
def conta_conciliar(request: HttpRequest, pk: int) -> HttpResponse:
    conta = get_object_or_404(Conta, pk=pk)
    ate_str = request.POST.get("ate") or hoje().isoformat()
    try:
        from datetime import date
        ate = date.fromisoformat(ate_str)
    except ValueError:
        ate = hoje()

    with transaction.atomic():
        qs = Lancamento.objects.filter(
            Q(conta=conta) | Q(conta_destino=conta),
            pago=True,
            conciliado=False,
            data_pagamento__lte=ate,
        )
        qtd = qs.update(conciliado=True, conciliado_em=timezone.now(), atualizado_por=request.user)
        conta.conciliado_ate = max(conta.conciliado_ate, ate) if conta.conciliado_ate else ate
        if request.POST.get("bloquear") == "1":
            conta.bloqueado_ate = max(conta.bloqueado_ate, ate) if conta.bloqueado_ate else ate
        conta.save(update_fields=["conciliado_ate", "bloqueado_ate"])
        registrar_auditoria(
            request.user,
            RegistroAuditoria.CONCILIAR,
            conta,
            f"Conciliou conta {conta.nome} até {ate:%d/%m/%Y} ({qtd} item(ns))",
            {"ate": ate.isoformat(), "qtd": qtd, "bloqueou": request.POST.get("bloquear") == "1"},
            request=request,
        )
    messages.success(request, f"Conta conciliada até {ate:%d/%m/%Y} ({qtd} lançamento(s) marcado(s)).")
    return redirect("conta_extrato", pk=conta.pk)


@login_required
def fechamento_mensal(request: HttpRequest) -> HttpResponse:
    ini = parse_mes(request.GET.get("mes"))
    fim = fim_mes(ini)
    fechamento = FechamentoMensal.objects.filter(mes=ini).first()

    qs_mes = sv.lancamentos_do_mes(ini)
    receitas = sv.soma(qs_mes.filter(tipo=Lancamento.RECEITA))
    despesas = sv.despesas_liquidas(qs_mes)
    pendentes_mes = qs_mes.filter(pago=False).exclude(tipo=Lancamento.PAGAMENTO_FATURA).count()
    nao_conciliados = qs_mes.filter(pago=True, cartao__isnull=True, conciliado=False).count()
    faturas_abertas = Fatura.objects.filter(referencia=ini, paga=False).count()

    if request.method == "POST":
        acao = request.POST.get("acao")
        if acao == "fechar":
            form = FechamentoForm(request.POST, instance=fechamento)
            if form.is_valid():
                obj = form.save(commit=False)
                obj.mes = ini
                obj.fechado = True
                obj.fechado_por = request.user
                obj.fechado_em = timezone.now()
                obj.resumo_snapshot = {
                    "receitas": str(receitas),
                    "despesas": str(despesas),
                    "resultado": str(receitas - despesas),
                    "pendentes_mes": pendentes_mes,
                    "nao_conciliados": nao_conciliados,
                }
                obj.save()
                registrar_auditoria(
                    request.user,
                    RegistroAuditoria.ADMIN,
                    obj,
                    f"Fechou mês {ini:%m/%Y}",
                    obj.resumo_snapshot,
                    request=request,
                )
                messages.success(request, f"Mês {ini:%m/%Y} fechado com sucesso.")
                return redirect(f"{request.path}?mes={mes_str(ini)}")
        elif acao == "reabrir" and fechamento:
            fechamento.fechado = False
            fechamento.save(update_fields=["fechado"])
            registrar_auditoria(
                request.user,
                RegistroAuditoria.ADMIN,
                fechamento,
                f"Reabriu mês {ini:%m/%Y}",
                request=request,
            )
            messages.info(request, f"Mês {ini:%m/%Y} reaberto para edições.")
            return redirect(f"{request.path}?mes={mes_str(ini)}")
    else:
        form = FechamentoForm(instance=fechamento)

    return render(
        request,
        "financeiro/fechamento.html",
        {
            "nav": _nav_mes(request, ini),
            "fechamento": fechamento,
            "form": form,
            "receitas": receitas,
            "despesas": despesas,
            "resultado": receitas - despesas,
            "pendentes_mes": pendentes_mes,
            "nao_conciliados": nao_conciliados,
            "faturas_abertas": faturas_abertas,
            "fim": fim,
            "historico": FechamentoMensal.objects.select_related("fechado_por").order_by("-mes")[:12],
        },
    )
