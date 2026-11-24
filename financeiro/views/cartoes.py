from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .. import services as sv
from ..forms import PagamentoFaturaForm
from ..models import Cartao, Fatura
from ..utils import hoje, parse_mes, somar_meses
from ._comum import ZERO


@login_required
def cartoes(request: HttpRequest) -> HttpResponse:
    resumo = sv.resumo_cartoes()
    cartoes_lista = [item["cartao"] for item in resumo]
    faturas_por_cartao: dict[int, list[Fatura]] = {c.pk: [] for c in cartoes_lista}
    if cartoes_lista:
        limite_passado = somar_meses(hoje().replace(day=1), -4, dia=1)
        todas = (
            Fatura.objects.com_totais()
            .filter(cartao__in=cartoes_lista)
            .filter(Q(paga=False) | Q(referencia__gte=limite_passado))
            .order_by("referencia")
        )
        for f in todas:
            faturas_por_cartao[f.cartao_id].append(f)

    dados = []
    for item in resumo:
        c = item["cartao"]
        faturas = [
            f for f in faturas_por_cartao.get(c.pk, [])
            if f.total != ZERO or f.referencia == item["atual"].referencia
        ]
        item["faturas"] = faturas
        dados.append(item)
    return render(request, "financeiro/cartoes.html", {"dados": dados})


def _render_fatura(request: HttpRequest, f: Fatura) -> HttpResponse:
    itens = (
        f.lancamentos.select_related("categoria__pai", "centro", "criado_por").order_by("data", "id")
        if f.pk
        else []
    )
    form = PagamentoFaturaForm(
        initial={
            "conta": f.cartao.conta_pagamento_id,
            "valor": max(f.saldo, ZERO),
            "data": hoje(),
        }
    )
    anterior = Fatura.objects.filter(cartao=f.cartao, referencia__lt=f.referencia).order_by("-referencia").first()
    proxima = Fatura.objects.filter(cartao=f.cartao, referencia__gt=f.referencia).order_by("referencia").first()
    anterior_ref = anterior.referencia if anterior else somar_meses(f.referencia, -1, dia=1)
    proxima_ref = proxima.referencia if proxima else somar_meses(f.referencia, 1, dia=1)
    pagamentos = f.pagamentos.select_related("conta", "criado_por").order_by("data_pagamento") if f.pk else []
    por_categoria = sv.despesas_por_categoria(f.lancamentos.all()) if f.pk else []
    return render(
        request,
        "financeiro/fatura.html",
        {
            "f": f,
            "itens": itens,
            "form": form,
            "pagamentos": pagamentos,
            "por_categoria": por_categoria,
            "anterior": anterior,
            "anterior_ref": anterior_ref,
            "proxima": proxima,
            "proxima_ref": proxima_ref,
        },
    )


@login_required
def fatura(request: HttpRequest, pk: int) -> HttpResponse:
    f = get_object_or_404(Fatura.objects.com_totais().select_related("cartao__conta_pagamento"), pk=pk)
    return _render_fatura(request, f)


@login_required
def fatura_mes(request: HttpRequest, cartao_pk: int, ref: str) -> HttpResponse:
    cartao = get_object_or_404(Cartao.objects.select_related("conta_pagamento"), pk=cartao_pk)
    referencia = parse_mes(ref)
    f = Fatura.objects.com_totais().filter(cartao=cartao, referencia=referencia).first()
    if f is None:
        f = cartao.fatura_virtual(referencia)
    return _render_fatura(request, f)


@login_required
@require_POST
def fatura_pagar(request: HttpRequest, pk: int) -> HttpResponse:
    f = get_object_or_404(Fatura.objects.select_related("cartao"), pk=pk)
    form = PagamentoFaturaForm(request.POST)
    if form.is_valid():
        cd = form.cleaned_data
        try:
            sv.pagar_fatura(
                f.pk,
                conta=cd["conta"],
                valor=cd["valor"],
                data=cd["data"],
                usuario=request.user,
                request=request,
            )
            messages.success(request, "Pagamento registrado.")
        except ValidationError as exc:
            for msg in exc.messages:
                messages.error(request, msg)
    else:
        erros = "; ".join(f"{e}" for lista in form.errors.values() for e in lista)
        messages.error(request, f"Não foi possível registrar o pagamento: {erros}")
    return redirect("fatura", pk=f.pk)
