from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import services as sv
from ..audit import registrar_auditoria
from ..forms import MetaFinanceiraForm, OrcamentoForm
from ..models import MetaFinanceira, Orcamento, RegistroAuditoria
from ..utils import parse_mes
from ._comum import _nav_mes, _render_form


@login_required
def orcamento(request: HttpRequest) -> HttpResponse:
    ini = parse_mes(request.GET.get("mes"))
    itens = sv.orcamento_mes(ini)
    total_limite, total_gasto = sv.totais_orcamento(itens)
    return render(
        request,
        "financeiro/orcamento.html",
        {
            "nav": _nav_mes(request, ini),
            "itens": itens,
            "total_limite": total_limite,
            "total_gasto": total_gasto,
            "inativos": Orcamento.objects.filter(ativo=False).select_related("categoria__pai", "centro"),
        },
    )


@login_required
def orcamento_form(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    inst = get_object_or_404(Orcamento, pk=pk) if pk else None
    form = OrcamentoForm(request.POST or None, instance=inst)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        registrar_auditoria(
            request.user,
            RegistroAuditoria.EDITAR if inst else RegistroAuditoria.CRIAR,
            obj,
            f"{'Editou' if inst else 'Criou'} orçamento: {obj.categoria} (R$ {obj.valor_mensal})",
            request=request,
        )
        messages.success(request, "Orçamento salvo.")
        return redirect("orcamento")
    extra = {"excluir_url": reverse("orcamento_excluir", args=[inst.pk])} if inst else {}
    return _render_form(
        request, form, "Editar orçamento" if inst else "Novo limite de orçamento", reverse("orcamento"), **extra
    )


@login_required
@require_POST
def orcamento_excluir(request: HttpRequest, pk: int) -> HttpResponse:
    obj = get_object_or_404(Orcamento, pk=pk)
    registrar_auditoria(
        request.user, RegistroAuditoria.EXCLUIR, obj, f"Excluiu orçamento: {obj}", request=request
    )
    obj.delete()
    messages.success(request, "Orçamento excluído.")
    return redirect("orcamento")


# ---------------------------------------------------------------------------
# Metas financeiras
# ---------------------------------------------------------------------------


@login_required
def metas(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "financeiro/metas.html",
        {
            "metas": sv.resumo_metas(),
            "inativas": MetaFinanceira.objects.filter(ativo=False),
        },
    )


@login_required
def meta_form(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    inst = get_object_or_404(MetaFinanceira, pk=pk) if pk else None
    form = MetaFinanceiraForm(request.POST or None, instance=inst)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        registrar_auditoria(
            request.user,
            RegistroAuditoria.EDITAR if inst else RegistroAuditoria.CRIAR,
            obj,
            f"{'Editou' if inst else 'Criou'} meta: {obj.nome} (R$ {obj.valor_alvo})",
            request=request,
        )
        messages.success(request, "Meta salva.")
        return redirect("metas")
    extra = {"excluir_url": reverse("meta_excluir", args=[inst.pk])} if inst else {}
    return _render_form(
        request, form, "Editar meta" if inst else "Nova meta financeira", reverse("metas"), **extra
    )


@login_required
@require_POST
def meta_excluir(request: HttpRequest, pk: int) -> HttpResponse:
    obj = get_object_or_404(MetaFinanceira, pk=pk)
    registrar_auditoria(request.user, RegistroAuditoria.EXCLUIR, obj, f"Excluiu meta: {obj.nome}", request=request)
    obj.delete()
    messages.success(request, "Meta excluída.")
    return redirect("metas")
