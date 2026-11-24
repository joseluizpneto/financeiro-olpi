from __future__ import annotations

from collections import OrderedDict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import ProtectedError
from django.forms import modelform_factory
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import services as sv
from ..audit import registrar_auditoria
from ..forms import ConfigForm, RegraCategorizacaoForm, categorias_qs
from ..models import (
    Cartao, Categoria, CentroCusto, Conta, RegistroAuditoria, RegraCategorizacao, Titular, Veiculo,
)
from ._comum import _render_form, _voltar

CONFIG = OrderedDict([
    ("titulares", {
        "modelo": Titular,
        "titulo": "Titulares",
        "icone": "people",
        "descricao": "Pessoas (e empresas, se houver) donas das contas e cartões.",
        "campos": ["nome", "tipo", "usuario", "telegram_chat_id", "ativo"],
        "colunas": ["nome", "tipo", "usuario", "ativo"],
        "relacionados": ["usuario"],
    }),
    ("contas", {
        "modelo": Conta,
        "titulo": "Contas bancárias",
        "icone": "bank",
        "descricao": "Contas correntes, poupanças, carteira em dinheiro.",
        "campos": [
            "nome", "banco", "tipo", "titular", "saldo_inicial", "data_saldo_inicial",
            "centro_padrao", "conciliado_ate", "bloqueado_ate", "ativo",
        ],
        "colunas": ["nome", "banco", "titular", "tipo", "saldo_atual", "ativo"],
        "relacionados": ["titular"],
    }),
    ("cartoes", {
        "modelo": Cartao,
        "titulo": "Cartões de crédito",
        "icone": "credit-card",
        "descricao": "Limite, dia de fechamento e de vencimento.",
        "campos": [
            "nome", "banco", "titular", "limite", "dia_fechamento", "dia_vencimento",
            "conta_pagamento", "centro_padrao", "ativo",
        ],
        "colunas": ["nome", "titular", "limite", "dia_fechamento", "dia_vencimento", "ativo"],
        "relacionados": ["titular"],
    }),
    ("categorias", {
        "modelo": Categoria,
        "titulo": "Categorias",
        "icone": "tags",
        "descricao": "Receitas e despesas, com subcategorias.",
        "campos": ["nome", "tipo", "pai", "imposto", "ativo"],
        "colunas": ["__str__", "tipo", "imposto", "ativo"],
    }),
    ("centros", {
        "modelo": CentroCusto,
        "titulo": "Centros de custo",
        "icone": "diagram-3",
        "descricao": "Separa Casa, Veículo e empresas (opcional) nos relatórios.",
        "campos": ["nome", "descricao", "empresa", "ativo"],
        "colunas": ["nome", "descricao", "empresa", "ativo"],
    }),
    ("veiculos", {
        "modelo": Veiculo,
        "titulo": "Veículos",
        "icone": "car-front",
        "descricao": "Dados do veículo e hodômetro inicial.",
        "campos": ["nome", "marca", "modelo", "ano", "placa", "km_inicial", "centro", "ativo"],
        "colunas": ["nome", "modelo", "placa", "km_inicial", "ativo"],
    }),
])


def _config(slug: str) -> dict:
    cfg = CONFIG.get(slug)
    if cfg is None:
        raise Http404
    return cfg


@login_required
def config_index(request: HttpRequest) -> HttpResponse:
    itens = [{"slug": s, **c, "total": c["modelo"].objects.count()} for s, c in CONFIG.items()]
    return render(
        request,
        "financeiro/config_index.html",
        {
            "itens": itens,
            "total_regras": RegraCategorizacao.objects.count(),
        },
    )


@login_required
def config_lista(request: HttpRequest, slug: str) -> HttpResponse:
    cfg = _config(slug)
    modelo = cfg["modelo"]
    qs = modelo.objects.select_related(*cfg.get("relacionados", []))
    if modelo is Categoria:
        qs = categorias_qs()
    objetos = list(qs)
    if modelo is Conta:
        objetos = sv.saldos_contas(objetos)
    colunas = []
    for nome in cfg["colunas"]:
        if nome == "__str__":
            colunas.append((nome, "Nome"))
        elif nome == "saldo_atual":
            colunas.append((nome, "Saldo atual"))
        else:
            colunas.append((nome, modelo._meta.get_field(nome).verbose_name))
    return render(
        request,
        "financeiro/config_lista.html",
        {"slug": slug, "cfg": cfg, "objetos": objetos, "colunas": colunas},
    )


@login_required
def config_form(request: HttpRequest, slug: str, pk: int | None = None) -> HttpResponse:
    cfg = _config(slug)
    modelo = cfg["modelo"]
    inst = get_object_or_404(modelo, pk=pk) if pk else None
    Form = modelform_factory(modelo, form=ConfigForm, fields=cfg["campos"])
    form = Form(request.POST or None, instance=inst)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        registrar_auditoria(
            request.user,
            RegistroAuditoria.EDITAR if inst else RegistroAuditoria.CRIAR,
            obj,
            f"{'Editou' if inst else 'Criou'} {modelo._meta.verbose_name}: {obj}",
            request=request,
        )
        messages.success(request, "Cadastro salvo.")
        return redirect(_voltar(request, reverse("config_lista", args=[slug])))
    extra = {"excluir_url": reverse("config_excluir", args=[slug, inst.pk])} if inst else {}
    singular = modelo._meta.verbose_name
    titulo = f"Editar {singular}" if inst else f"Novo cadastro: {singular}"
    return _render_form(request, form, titulo, reverse("config_lista", args=[slug]), **extra)


@login_required
@require_POST
def config_excluir(request: HttpRequest, slug: str, pk: int) -> HttpResponse:
    cfg = _config(slug)
    obj = get_object_or_404(cfg["modelo"], pk=pk)
    try:
        nome_obj = str(obj)
        obj.delete()
        registrar_auditoria(
            request.user,
            RegistroAuditoria.EXCLUIR,
            cfg["modelo"]._meta.model_name,
            f"Excluiu {cfg['modelo']._meta.verbose_name}: {nome_obj}",
            request=request,
        )
        messages.success(request, "Cadastro excluído.")
    except ProtectedError:
        messages.error(
            request,
            "Este cadastro já está em uso. Desmarque “ativo” para escondê-lo sem perder o histórico.",
        )
        return redirect("config_editar", slug=slug, pk=pk)
    return redirect("config_lista", slug=slug)


# ---------------------------------------------------------------------------
# Regras de categorização automática (importação OFX/CSV)
# ---------------------------------------------------------------------------


@login_required
def regras_lista(request: HttpRequest) -> HttpResponse:
    itens = RegraCategorizacao.objects.select_related("categoria__pai", "centro")
    return render(request, "financeiro/regras.html", {"itens": itens})


@login_required
def regra_form(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    inst = get_object_or_404(RegraCategorizacao, pk=pk) if pk else None
    form = RegraCategorizacaoForm(request.POST or None, instance=inst)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        registrar_auditoria(
            request.user,
            RegistroAuditoria.EDITAR if inst else RegistroAuditoria.CRIAR,
            obj,
            f"{'Editou' if inst else 'Criou'} regra de categorização: {obj}",
            request=request,
        )
        messages.success(request, "Regra salva.")
        return redirect("regras_lista")
    extra = {"js_forma": True}
    if inst:
        extra["excluir_url"] = reverse("regra_excluir", args=[inst.pk])
    return _render_form(
        request, form, "Editar regra" if inst else "Nova regra de categorização", reverse("regras_lista"), **extra
    )


@login_required
@require_POST
def regra_excluir(request: HttpRequest, pk: int) -> HttpResponse:
    obj = get_object_or_404(RegraCategorizacao, pk=pk)
    registrar_auditoria(
        request.user, RegistroAuditoria.EXCLUIR, obj, f"Excluiu regra: {obj}", request=request
    )
    obj.delete()
    messages.success(request, "Regra excluída.")
    return redirect("regras_lista")
