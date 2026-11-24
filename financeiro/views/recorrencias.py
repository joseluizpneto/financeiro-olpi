from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import services as sv
from ..audit import diff_snapshots, registrar_auditoria, snapshot_instancia
from ..forms import RecorrenciaForm
from ..models import Lancamento, Recorrencia, RegistroAuditoria
from ..utils import hoje
from ._comum import ZERO, _render_form

CAMPOS_RECORRENCIA = ["descricao", "tipo", "valor", "intervalo_meses", "dia", "conta", "cartao", "categoria", "centro", "ativo"]


@login_required
def recorrencias(request: HttpRequest) -> HttpResponse:
    itens = list(
        Recorrencia.objects.select_related("categoria__pai", "conta__titular", "cartao", "centro", "criado_por", "atualizado_por")
    )
    for r in itens:
        r.proxima_data = r.proxima() if r.ativo else None
    ativas = [r for r in itens if r.ativo]
    return render(
        request,
        "financeiro/recorrencias.html",
        {
            "itens": itens,
            "mensal_despesas": sum((r.valor_mensal for r in ativas if r.tipo == "D"), ZERO),
            "mensal_receitas": sum((r.valor_mensal for r in ativas if r.tipo == "R"), ZERO),
        },
    )


@login_required
def recorrencia_form(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    inst = get_object_or_404(Recorrencia, pk=pk) if pk else None
    initial = {} if inst else {"tipo": request.GET.get("tipo", "D"), "inicio": hoje(), "dia": hoje().day}
    antes = snapshot_instancia(inst, CAMPOS_RECORRENCIA) if inst else {}
    form = RecorrenciaForm(request.POST or None, instance=inst, initial=initial)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            r = form.save(commit=False)
            if inst is None:
                r.criado_por = request.user
            r.atualizado_por = request.user
            r.save()
            if inst:
                sv.sincronizar_recorrencia(r)
            if r.ativo:
                r.gerar(sv.horizonte_recorrencias())
            depois = snapshot_instancia(r, CAMPOS_RECORRENCIA)
            if inst:
                registrar_auditoria(
                    request.user,
                    RegistroAuditoria.EDITAR,
                    r,
                    f"Editou recorrência: {r.descricao} (R$ {r.valor})",
                    {"alteracoes": diff_snapshots(antes, depois)},
                    request=request,
                )
            else:
                registrar_auditoria(
                    request.user,
                    RegistroAuditoria.CRIAR,
                    r,
                    f"Nova recorrência: {r.descricao} (R$ {r.valor})",
                    {"dados": depois},
                    request=request,
                )
        messages.success(request, "Recorrência salva. Os próximos lançamentos já aparecem em “A vencer”.")
        return redirect("recorrencias")
    extra = {"js_forma": True, "auditoria_obj": inst}
    if inst:
        extra["excluir_url"] = reverse("recorrencia_excluir", args=[inst.pk])
        extra["aviso"] = "Ao salvar, os lançamentos futuros ainda não pagos são atualizados."
    return _render_form(
        request,
        form,
        "Editar recorrência" if inst else "Nova recorrência",
        reverse("recorrencias"),
        **extra,
    )


@login_required
@require_POST
def recorrencia_excluir(request: HttpRequest, pk: int) -> HttpResponse:
    r = get_object_or_404(Recorrencia, pk=pk)
    with transaction.atomic():
        futuros = Lancamento.objects.filter(recorrencia=r, data__gte=hoje()).filter(
            Q(pago=False) | Q(cartao__isnull=False, fatura__paga=False)
        )
        for item in futuros:
            item.delete()
        registrar_auditoria(
            request.user,
            RegistroAuditoria.EXCLUIR,
            r,
            f"Excluiu recorrência: {r.descricao} (R$ {r.valor})",
            request=request,
        )
        r.delete()
    messages.success(request, "Recorrência excluída (os lançamentos já pagos e passados foram mantidos).")
    return redirect("recorrencias")
