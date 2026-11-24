from __future__ import annotations

from datetime import timedelta

from django.http import HttpRequest, JsonResponse

from .. import services as sv
from ..models import Conta
from ..security import autenticar_token_api
from ..utils import hoje, inicio_mes


def api_resumo(request: HttpRequest) -> JsonResponse:
    tok = autenticar_token_api(request)
    if tok is None:
        return JsonResponse({"erro": "Token inválido ou ausente."}, status=401)

    h = hoje()
    ini = inicio_mes(h)
    contas = sv.saldos_contas(Conta.objects.filter(ativo=True).select_related("titular"), ate=h)
    qs_mes = sv.lancamentos_do_mes(ini)
    receitas = sv.soma(qs_mes.filter(tipo="R"))
    despesas = sv.despesas_liquidas(qs_mes)
    pendentes, faturas = sv.contas_a_vencer(h + timedelta(days=7))
    return JsonResponse({
        "data": h.isoformat(),
        "saldo_total": str(sum((c.saldo_atual for c in contas), sv.ZERO)),
        "contas": [
            {"id": c.pk, "nome": c.nome, "titular": c.titular.nome, "saldo": str(c.saldo_atual)}
            for c in contas
        ],
        "mes": {
            "referencia": ini.strftime("%Y-%m"),
            "receitas": str(receitas),
            "despesas": str(despesas),
            "resultado": str(receitas - despesas),
        },
        "a_vencer_7d": {
            "pendentes": [
                {"descricao": l.descricao, "valor": str(l.valor), "vencimento": l.vencimento.isoformat(), "tipo": l.tipo}
                for l in pendentes
            ],
            "faturas": [
                {"cartao": f.cartao.nome, "saldo": str(f.saldo), "vencimento": f.vencimento.isoformat()}
                for f in faturas
            ],
        },
        "metas": [
            {"nome": m["m"].nome, "atual": str(m["atual"]), "alvo": str(m["m"].valor_alvo), "pct": m["pct"]}
            for m in sv.resumo_metas()
        ],
    })


def manifest_pwa(request: HttpRequest) -> JsonResponse:
    return JsonResponse(
        {
            "name": "Controle Financeiro",
            "short_name": "Finanças",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#f8fafc",
            "theme_color": "#0f766e",
            "lang": "pt-BR",
        },
        content_type="application/manifest+json",
    )
