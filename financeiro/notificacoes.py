from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import timedelta
from typing import Any

from .models import Integracao, Veiculo
from .services import alertas_manutencao, contas_a_vencer, orcamento_mes
from .utils import hoje, inicio_mes


def _config_alertas() -> dict[str, Any]:
    integ = Integracao.objects.filter(provedor=Integracao.ALERTAS).first()
    cfg = integ.config if integ else {}
    return {
        "ativo": integ.ativo if integ else True,
        "dias_vencimento": int(cfg.get("dias_vencimento") or 3),
        "orcamento_pct": int(cfg.get("orcamento_pct") or 80),
        "incluir_veiculos": bool(cfg.get("incluir_veiculos", True)),
    }


def montar_alertas() -> dict[str, Any]:
    cfg = _config_alertas()
    h = hoje()
    ate = h + timedelta(days=cfg["dias_vencimento"])
    pendentes, faturas = contas_a_vencer(ate)
    orcamentos = [o for o in orcamento_mes(inicio_mes(h)) if o["pct"] >= cfg["orcamento_pct"]]
    alertas_veiculos = []
    if cfg["incluir_veiculos"]:
        for v in Veiculo.objects.filter(ativo=True):
            for a in alertas_manutencao(v):
                alertas_veiculos.append({"veiculo": v, **a})

    total = len(pendentes) + len(faturas) + len(orcamentos) + len(alertas_veiculos)
    return {
        "hoje": h,
        "dias_vencimento": cfg["dias_vencimento"],
        "pendentes": pendentes,
        "faturas": faturas,
        "orcamentos": orcamentos,
        "veiculos": alertas_veiculos,
        "total": total,
    }


def formatar_mensagem_alertas(resumo: dict[str, Any]) -> str:
    if resumo["total"] == 0:
        return "Tudo em ordem hoje! Nenhuma pendência ou alerta."
    linhas = [f"Controle Financeiro · Resumo de {resumo['hoje']:%d/%m/%Y}"]
    if resumo["faturas"]:
        linhas.append("\nFaturas de cartão:")
        for f in resumo["faturas"]:
            linhas.append(f"- {f.cartao.nome} (vence {f.vencimento:%d/%m}): R$ {f.saldo:.2f}")
    if resumo["pendentes"]:
        linhas.append(f"\nContas atrasadas ou até {resumo['dias_vencimento']} dia(s):")
        for l in resumo["pendentes"]:
            status = "ATRASADO" if l.vencimento < resumo["hoje"] else f"{l.vencimento:%d/%m}"
            linhas.append(f"- [{status}] {l.descricao}: R$ {l.valor:.2f}")
    if resumo["orcamentos"]:
        linhas.append("\nOrçamento em atenção:")
        for o in resumo["orcamentos"]:
            linhas.append(f"- {o['o'].categoria}: {o['pct']}% (R$ {o['gasto']:.2f} / R$ {o['o'].valor_mensal:.2f})")
    if resumo["veiculos"]:
        linhas.append("\nManutenção de veículos:")
        for a in resumo["veiculos"]:
            linhas.append(f"- {a['veiculo'].nome} · {a['manutencao'].servico}: {a['texto']}")
    return "\n".join(linhas)


def obter_credenciais_telegram() -> tuple[str, str, bool]:
    integ = Integracao.objects.filter(provedor=Integracao.TELEGRAM).first()
    token = (integ.segredo if integ and integ.segredo else "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = ((integ.config or {}).get("chat_id") if integ else "") or os.environ.get("TELEGRAM_CHAT_ID", "")
    ativo = bool(integ.ativo) if integ else bool(token and chat_id)
    return token.strip(), str(chat_id).strip(), ativo


def enviar_telegram(texto: str, chat_id: str | None = None, token: str | None = None) -> tuple[bool, str]:
    tok_cfg, chat_cfg, _ = obter_credenciais_telegram()
    tok = (token or tok_cfg).strip()
    cid = (chat_id or chat_cfg).strip()
    if not tok or not cid:
        return False, "Token do Bot ou Chat ID do Telegram não configurados."

    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    payload = json.dumps({"chat_id": cid, "text": texto}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True, "Mensagem enviada com sucesso pelo Telegram."
            return False, f"Telegram retornou status HTTP {resp.status}."
    except urllib.error.HTTPError as exc:
        return False, f"Erro HTTP {exc.code} ao enviar no Telegram."
    except Exception as exc:
        return False, f"Falha de conexão com o Telegram: {exc}"


def enviar_alertas(dry_run: bool = False) -> dict[str, Any]:
    resumo = montar_alertas()
    mensagem = formatar_mensagem_alertas(resumo)
    if dry_run or resumo["total"] == 0:
        return {"enviado": False, "motivo": "dry_run" if dry_run else "sem_alertas", "mensagem": mensagem, "total": resumo["total"]}
    _, _, ativo = obter_credenciais_telegram()
    if not ativo:
        return {"enviado": False, "motivo": "telegram_inativo", "mensagem": mensagem, "total": resumo["total"]}
    ok, detalhe = enviar_telegram(mensagem)
    return {"enviado": ok, "motivo": detalhe, "mensagem": mensagem, "total": resumo["total"]}
