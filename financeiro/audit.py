from __future__ import annotations

import ipaddress
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import models
from django.db.models.fields.files import FieldFile
from django.http import HttpRequest

from .models import RegistroAuditoria

CAMPOS_LANCAMENTO = [
    "tipo",
    "descricao",
    "valor",
    "data",
    "vencimento",
    "pago",
    "data_pagamento",
    "conta",
    "conta_destino",
    "cartao",
    "fatura",
    "fatura_paga",
    "categoria",
    "centro",
    "veiculo",
    "compartilhado",
    "pago_por",
    "conciliado",
    "observacao",
]


def obter_ip(request: HttpRequest | None) -> str | None:
    if request is None:
        return None
    candidato = ""
    if getattr(settings, "SESSION_COOKIE_SECURE", False):
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if xff:
            candidato = xff.split(",")[0].strip()
    if not candidato:
        candidato = (request.META.get("REMOTE_ADDR") or "").strip()
    if not candidato:
        return None
    try:
        return str(ipaddress.ip_address(candidato))
    except ValueError:
        return None


def _serializar_valor(valor: Any) -> Any:
    if valor is None:
        return None
    if isinstance(valor, Decimal):
        return str(valor)
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    if isinstance(valor, FieldFile):
        return valor.name or ""
    if isinstance(valor, models.Model):
        return str(valor)
    return valor


def snapshot_instancia(obj: models.Model, campos: list[str]) -> dict[str, Any]:
    dados: dict[str, Any] = {}
    for campo in campos:
        if hasattr(obj, campo):
            dados[campo] = _serializar_valor(getattr(obj, campo))
    return dados


def diff_snapshots(antes: dict[str, Any], depois: dict[str, Any]) -> dict[str, dict[str, Any]]:
    chaves = set(antes) | set(depois)
    alteracoes: dict[str, dict[str, Any]] = {}
    for k in sorted(chaves):
        va, vd = antes.get(k), depois.get(k)
        if va != vd:
            alteracoes[k] = {"antes": va, "depois": vd}
    return alteracoes


def registrar_auditoria(
    usuario: Any,
    acao: str,
    obj_ou_modelo: models.Model | str,
    resumo: str,
    detalhes: dict[str, Any] | None = None,
    request: HttpRequest | None = None,
) -> RegistroAuditoria:
    user_obj = usuario if getattr(usuario, "is_authenticated", False) else None
    if isinstance(obj_ou_modelo, models.Model):
        modelo = obj_ou_modelo._meta.model_name
        objeto_id = str(obj_ou_modelo.pk or "")
    else:
        modelo = str(obj_ou_modelo)
        objeto_id = ""
    return RegistroAuditoria.objects.create(
        usuario=user_obj,
        acao=acao,
        modelo=modelo[:60],
        objeto_id=objeto_id[:64],
        resumo=(resumo or "")[:255],
        detalhes=detalhes or {},
        ip=obter_ip(request),
    )
