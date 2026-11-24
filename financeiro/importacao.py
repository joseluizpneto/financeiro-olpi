from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from .audit import registrar_auditoria
from .models import (
    Cartao, Categoria, CentroCusto, Conta, Lancamento, RegistroAuditoria, RegraCategorizacao,
)
from .services import verificar_bloqueio_lancamento

_RE_STMTTRN = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.IGNORECASE | re.DOTALL)


def _decodificar(conteudo: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return conteudo.decode(enc)
        except UnicodeDecodeError:
            continue
    return conteudo.decode("utf-8", errors="replace")


def _normalizar_texto(s: str) -> str:
    sem_acento = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sem_acento.strip().lower())


def _parse_data(bruto: str) -> date | None:
    s = (bruto or "").strip()
    if not s:
        return None
    # OFX YYYYMMDD...
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", s)
    if m and "-" not in s[:10] and "/" not in s[:10]:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _parse_valor(bruto: str) -> Decimal | None:
    s = (bruto or "").strip()
    if not s:
        return None
    negativo = False
    if s.startswith("(") and s.endswith(")"):
        negativo = True
        s = s[1:-1].strip()
    s = re.sub(r"[R$\s]", "", s)
    if s.startswith("-"):
        negativo = True
        s = s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        v = Decimal(s).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return -v if negativo else v


def _extrair_tag_ofx(bloco: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\s*([^\r\n<]+)", bloco, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_ofx(texto: str) -> list[dict[str, Any]]:
    itens: list[dict[str, Any]] = []
    blocos = _RE_STMTTRN.findall(texto)
    if not blocos and "<STMTTRN>" in texto.upper():
        blocos = re.split(r"<STMTTRN>", texto, flags=re.IGNORECASE)[1:]
    for bloco in blocos:
        dt = _parse_data(_extrair_tag_ofx(bloco, "DTPOSTED"))
        val = _parse_valor(_extrair_tag_ofx(bloco, "TRNAMT"))
        if dt is None or val is None or val == Decimal("0.00"):
            continue
        trntype = _extrair_tag_ofx(bloco, "TRNTYPE").upper()
        fitid = _extrair_tag_ofx(bloco, "FITID")
        memo = _extrair_tag_ofx(bloco, "MEMO") or _extrair_tag_ofx(bloco, "NAME") or "Lançamento importado"
        tipo = Lancamento.DESPESA if (val < 0 or trntype in ("DEBIT", "POS", "FEE", "ATM")) and val <= 0 else Lancamento.RECEITA
        if val < 0:
            tipo = Lancamento.DESPESA
        elif trntype == "CREDIT" or val > 0:
            tipo = Lancamento.RECEITA
        itens.append({
            "data": dt,
            "valor": abs(val),
            "tipo": tipo,
            "descricao": re.sub(r"\s+", " ", memo)[:200],
            "ref": fitid,
        })
    return itens


def parse_csv_extrato(texto: str) -> list[dict[str, Any]]:
    linhas = [ln for ln in texto.splitlines() if ln.strip()]
    if not linhas:
        return []
    amostra = "\n".join(linhas[:5])
    delimitador = ";" if amostra.count(";") >= amostra.count(",") else ","
    reader = csv.reader(io.StringIO("\n".join(linhas)), delimiter=delimitador)
    rows = list(reader)
    if len(rows) < 2:
        return []

    cab = [_normalizar_texto(c) for c in rows[0]]

    def _achar_col(candidatos: tuple[str, ...]) -> int | None:
        for i, h in enumerate(cab):
            if any(c == h or c in h for c in candidatos):
                return i
        return None

    col_data = _achar_col(("data", "date", "dt"))
    col_desc = _achar_col(("descricao", "historico", "title", "estabelecimento", "memo", "lancamento"))
    col_val = _achar_col(("valor", "amount", "quantia"))
    col_id = _achar_col(("identificador", "fitid", "id"))
    if col_data is None or col_desc is None or col_val is None:
        raise ValidationError(
            "Não foi possível identificar as colunas de Data, Descrição e Valor no CSV."
        )

    itens: list[dict[str, Any]] = []
    for row in rows[1:]:
        if len(row) <= max(col_data, col_desc, col_val):
            continue
        dt = _parse_data(row[col_data])
        val = _parse_valor(row[col_val])
        desc = re.sub(r"\s+", " ", row[col_desc]).strip()[:200]
        if dt is None or val is None or val == Decimal("0.00") or not desc:
            continue
        tipo = Lancamento.DESPESA if val < 0 else Lancamento.RECEITA
        ref = row[col_id].strip() if col_id is not None and col_id < len(row) else ""
        itens.append({
            "data": dt,
            "valor": abs(val),
            "tipo": tipo,
            "descricao": desc,
            "ref": ref,
        })
    return itens


def gerar_hash_importacao(destino_chave: str, data_tx: date, valor: Decimal, tipo: str, chave_unica: str) -> str:
    payload = f"{destino_chave}|{data_tx.isoformat()}|{valor:.2f}|{tipo}|{chave_unica.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def aplicar_regras_categorizacao(itens: list[dict[str, Any]]) -> None:
    regras = list(
        RegraCategorizacao.objects.filter(ativo=True)
        .select_related("categoria", "centro")
        .order_by("prioridade", "id")
    )
    for item in itens:
        desc_low = item["descricao"].lower()
        item.setdefault("categoria_id", None)
        item.setdefault("centro_id", None)
        for regra in regras:
            if regra.tipo == item["tipo"] and regra.padrao.strip().lower() in desc_low:
                item["categoria_id"] = regra.categoria_id
                item["centro_id"] = regra.centro_id
                break


def analisar_arquivo_extrato(
    arquivo,
    conta: Conta | None = None,
    cartao: Cartao | None = None,
    inverter_sinais_cartao: bool = False,
) -> list[dict[str, Any]]:
    if bool(conta) == bool(cartao):
        raise ValidationError("Escolha uma conta bancária ou um cartão de destino.")
    conteudo = arquivo.read()
    texto = _decodificar(conteudo)
    nome = (getattr(arquivo, "name", "") or "").lower()

    if nome.endswith((".ofx", ".qfx")) or "<OFX>" in texto.upper() or "<STMTTRN>" in texto.upper():
        brutos = parse_ofx(texto)
    else:
        brutos = parse_csv_extrato(texto)

    if not brutos:
        raise ValidationError("Nenhum lançamento válido encontrado no arquivo enviado.")

    destino_chave = f"cartao:{cartao.pk}" if cartao else f"conta:{conta.pk}"
    ocorrencias: Counter[tuple[date, Decimal, str, str]] = Counter()
    hashes: list[str] = []
    centro_padrao_id = cartao.centro_padrao_id if cartao else conta.centro_padrao_id

    for item in brutos:
        if cartao and inverter_sinais_cartao:
            item["tipo"] = Lancamento.RECEITA if item["tipo"] == Lancamento.DESPESA else Lancamento.DESPESA
        if item["ref"]:
            chave_unica = f"id:{item['ref']}"
        else:
            assinatura = (item["data"], item["valor"], item["tipo"], item["descricao"].lower())
            ocorrencias[assinatura] += 1
            chave_unica = f"desc:{item['descricao'].lower()}#{ocorrencias[assinatura]}"
        h = gerar_hash_importacao(destino_chave, item["data"], item["valor"], item["tipo"], chave_unica)
        item["hash"] = h
        item["centro_id"] = centro_padrao_id
        hashes.append(h)

    aplicar_regras_categorizacao(brutos)
    existentes = set(
        Lancamento.objects.filter(hash_importacao__in=hashes).values_list("hash_importacao", flat=True)
    )
    for item in brutos:
        item["ja_existe"] = item["hash"] in existentes
    return brutos


@transaction.atomic
def confirmar_importacao(
    itens: list[dict[str, Any]],
    *,
    conta: Conta | None = None,
    cartao: Cartao | None = None,
    usuario: Any = None,
    request: Any = None,
) -> tuple[int, int]:
    if bool(conta) == bool(cartao):
        raise ValidationError("Informe uma conta ou um cartão de destino.")

    hashes = [i["hash"] for i in itens if i.get("hash")]
    existentes = set(
        Lancamento.objects.filter(hash_importacao__in=hashes).values_list("hash_importacao", flat=True)
    )
    cats = {c.id: c for c in Categoria.objects.all()}
    centros = {c.id: c for c in CentroCusto.objects.all()}

    criados = 0
    ignorados = 0
    for item in itens:
        h = item.get("hash")
        if h and h in existentes:
            ignorados += 1
            continue
        data_tx = item["data"]
        msg_bloq = verificar_bloqueio_lancamento(data_tx, conta=conta)
        if msg_bloq:
            raise ValidationError(msg_bloq)

        tipo = item["tipo"]
        cat = cats.get(item.get("categoria_id"))
        if cat and ((tipo == Lancamento.ESTORNO and cat.tipo != Categoria.DESPESA) or (tipo in (Lancamento.RECEITA, Lancamento.DESPESA) and cat.tipo != tipo)):
            cat = None
        centro = centros.get(item.get("centro_id"))

        lanc = Lancamento(
            tipo=tipo,
            descricao=item["descricao"][:200],
            valor=item["valor"],
            data=data_tx,
            vencimento=data_tx,
            pago=True,
            data_pagamento=data_tx,
            conta=conta,
            cartao=cartao,
            categoria=cat,
            centro=centro,
            hash_importacao=h or None,
            criado_por=usuario,
            atualizado_por=usuario,
        )
        lanc.save()
        if h:
            existentes.add(h)
        criados += 1

    if criados:
        destino = str(cartao or conta)
        registrar_auditoria(
            usuario,
            RegistroAuditoria.IMPORTAR,
            "lancamento",
            f"Importação de {criados} lançamento(s) em {destino}",
            {"destino": destino, "criados": criados, "ignorados": ignorados},
            request=request,
        )
    return criados, ignorados
