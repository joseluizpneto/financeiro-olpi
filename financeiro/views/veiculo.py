from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import services as sv
from ..audit import registrar_auditoria
from ..forms import AbastecimentoForm, ManutencaoForm
from ..models import Abastecimento, Categoria, Lancamento, Manutencao, RegistroAuditoria, Veiculo
from ..utils import hoje
from ._comum import _ano, _render_form


@login_required
def veiculo(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    v = get_object_or_404(Veiculo, pk=pk) if pk else Veiculo.objects.filter(ativo=True).first()
    if v is None:
        return render(request, "financeiro/veiculo.html", {"v": None})
    ano = _ano(request)
    abastecimentos, media = sv.consumo_abastecimentos(v)
    cat_veiculo = (
        Categoria.objects.filter(tipo="D", codigo_sistema="veiculo", pai__isnull=True).first()
        or Categoria.objects.filter(tipo="D", nome__iexact="Veículo", pai__isnull=True).first()
    )
    return render(
        request,
        "financeiro/veiculo.html",
        {
            "v": v,
            "ano": ano,
            "km_atual": v.km_atual(),
            "media": media,
            "abastecimentos": abastecimentos[:20],
            "ultimo": abastecimentos[0] if abastecimentos else None,
            "manutencoes": v.manutencoes.select_related("lancamento")[:30],
            "alertas": sv.alertas_manutencao(v),
            "resumo": sv.resumo_veiculo(v, ano),
            "lancamentos": Lancamento.objects.filter(veiculo=v, data__year=ano)
            .select_related("categoria__pai", "conta", "cartao", "criado_por")
            .order_by("-data")[:50],
            "outros": Veiculo.objects.filter(ativo=True).exclude(pk=v.pk),
            "categoria_veiculo": cat_veiculo,
        },
    )


def _form_veiculo(request: HttpRequest, form_cls, veiculo_obj: Veiculo, inst, titulo: str, categoria: str, descricao):
    form = form_cls(request.POST or None, instance=inst, initial={} if inst else {"data": hoje()})
    voltar = reverse("veiculo_detalhe", args=[veiculo_obj.pk])
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        bloq = sv.verificar_bloqueio_lancamento(cd.get("data"), conta=cd.get("conta"))
        if not bloq and inst and inst.lancamento:
            bloq = sv.verificar_bloqueio_lancamento(inst.lancamento.data, conta=inst.lancamento.conta)
        if bloq:
            form.add_error(None, bloq)
        else:
            with transaction.atomic():
                obj = form.save(commit=False)
                obj.veiculo = veiculo_obj
                if inst is None:
                    obj.criado_por = request.user
                obj.atualizado_por = request.user
                obj.save()
                valor = getattr(obj, "valor_total", None)
                if valor is None:
                    valor = getattr(obj, "valor", None)
                sv.sincronizar_lancamento_veiculo(
                    obj,
                    descricao(obj),
                    valor,
                    categoria,
                    conta=cd.get("conta"),
                    cartao=cd.get("cartao"),
                    usuario=request.user,
                )
                registrar_auditoria(
                    request.user,
                    RegistroAuditoria.EDITAR if inst else RegistroAuditoria.CRIAR,
                    obj,
                    f"{'Editou' if inst else 'Registrou'} {titulo.lower()}: {descricao(obj)}",
                    {"veiculo": veiculo_obj.nome, "valor": str(valor)},
                    request=request,
                )
            messages.success(request, "Registro salvo e lançado no financeiro.")
            return redirect(voltar)
    extra = {"js_forma": True, "auditoria_obj": inst}
    if inst:
        nome = "abastecimento_excluir" if form_cls is AbastecimentoForm else "manutencao_excluir"
        extra["excluir_url"] = reverse(nome, args=[inst.pk])
    else:
        extra["aviso"] = f"Hodômetro atual registrado: {veiculo_obj.km_atual()} km."
    return _render_form(request, form, titulo, voltar, **extra)


@login_required
def abastecimento_form(request: HttpRequest, veiculo_pk: int | None = None, pk: int | None = None) -> HttpResponse:
    inst = get_object_or_404(Abastecimento, pk=pk) if pk else None
    v = inst.veiculo if inst else get_object_or_404(Veiculo, pk=veiculo_pk)
    return _form_veiculo(
        request,
        AbastecimentoForm,
        v,
        inst,
        "Abastecimento",
        "Combustível",
        lambda a: f"Abastecimento{' · ' + a.posto if a.posto else ''} ({a.litros} L)",
    )


@login_required
def manutencao_form(request: HttpRequest, veiculo_pk: int | None = None, pk: int | None = None) -> HttpResponse:
    inst = get_object_or_404(Manutencao, pk=pk) if pk else None
    v = inst.veiculo if inst else get_object_or_404(Veiculo, pk=veiculo_pk)
    return _form_veiculo(
        request,
        ManutencaoForm,
        v,
        inst,
        "Manutenção",
        "Manutenção",
        lambda m: f"Manutenção: {m.servico}",
    )


def _excluir_registro_veiculo(request: HttpRequest, modelo, pk: int) -> HttpResponse:
    obj = get_object_or_404(modelo, pk=pk)
    v = obj.veiculo
    if obj.lancamento:
        bloq = sv.verificar_bloqueio_lancamento(obj.lancamento.data, conta=obj.lancamento.conta)
        if bloq:
            messages.error(request, bloq)
            return redirect("veiculo_detalhe", pk=v.pk)
    with transaction.atomic():
        lanc = obj.lancamento
        registrar_auditoria(
            request.user,
            RegistroAuditoria.EXCLUIR,
            obj,
            f"Excluiu registro de {modelo._meta.verbose_name}: {obj}",
            request=request,
        )
        obj.delete()
        if lanc:
            lanc.delete()
    messages.success(request, "Registro e lançamento excluídos.")
    return redirect("veiculo_detalhe", pk=v.pk)


@login_required
@require_POST
def abastecimento_excluir(request: HttpRequest, pk: int) -> HttpResponse:
    return _excluir_registro_veiculo(request, Abastecimento, pk)


@login_required
@require_POST
def manutencao_excluir(request: HttpRequest, pk: int) -> HttpResponse:
    return _excluir_registro_veiculo(request, Manutencao, pk)
