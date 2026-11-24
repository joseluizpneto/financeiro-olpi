"""Regras de negócio e consultas usadas pelas telas."""
from __future__ import annotations

import uuid
from collections import OrderedDict, defaultdict
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import (
    Case, CharField, DecimalField as DBDecimalField, F, Max, Min, Q, Sum, Value, When,
)
from django.db.models.functions import Coalesce, TruncMonth
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone

from .audit import registrar_auditoria
from .models import (
    Cartao, Categoria, CentroCusto, Conta, Fatura, FechamentoMensal, Lancamento,
    MetaFinanceira, Orcamento, Recorrencia, RegistroAuditoria, Titular, Veiculo,
)
from .utils import dia_seguro, fim_mes, hoje, inicio_mes, somar_meses

ZERO = Decimal("0.00")
DEC_OUT = DBDecimalField(max_digits=12, decimal_places=2)
IMPOSTO = Q(categoria__imposto=True) | Q(categoria__pai__imposto=True)
CENTRO_EMPRESA = Q(centro__empresa=True)


def tem_empresa_ativa(request: Any | None = None) -> bool:
    """Indica se existe ao menos um centro de custo ativo marcado como empresa."""
    if request is not None and hasattr(request, "_tem_empresa_cache"):
        return bool(request._tem_empresa_cache)
    resultado = CentroCusto.objects.filter(empresa=True, ativo=True).exists()
    if request is not None:
        request._tem_empresa_cache = resultado
    return resultado


def _cache_key_recorrencias(dia: date | None = None) -> str:
    return f"recorrencias_geradas:{(dia or hoje()).isoformat()}"


@receiver([post_save, post_delete], sender=Recorrencia)
def _invalidar_cache_recorrencias(**kwargs: Any) -> None:
    cache.delete(_cache_key_recorrencias())


def soma(qs, campo: str = "valor") -> Decimal:
    return qs.aggregate(t=Sum(campo))["t"] or ZERO


def despesas_liquidas(qs) -> Decimal:
    """Despesas abatendo estornos/reembolsos."""
    res = qs.filter(tipo__in=[Lancamento.DESPESA, Lancamento.ESTORNO]).aggregate(
        t=Coalesce(
            Sum(
                Case(
                    When(tipo=Lancamento.DESPESA, then=F("valor")),
                    When(tipo=Lancamento.ESTORNO, then=-F("valor")),
                    default=ZERO,
                    output_field=DEC_OUT,
                )
            ),
            ZERO,
        )
    )["t"]
    return res or ZERO


def _por(qs, campo: str) -> dict[Any, Decimal]:
    return {r[campo]: r["t"] for r in qs.order_by().values(campo).annotate(t=Sum("valor"))}


def por_mes(qs, liquido_despesa: bool = False) -> dict[date, Decimal]:
    if liquido_despesa:
        expr = Sum(
            Case(
                When(tipo=Lancamento.DESPESA, then=F("valor")),
                When(tipo=Lancamento.ESTORNO, then=-F("valor")),
                default=ZERO,
                output_field=DEC_OUT,
            )
        )
        return {
            r["m"]: (r["t"] or ZERO)
            for r in qs.annotate(m=TruncMonth("data")).order_by().values("m").annotate(t=expr)
        }
    return {r["m"]: r["t"] for r in qs.annotate(m=TruncMonth("data")).order_by().values("m").annotate(t=Sum("valor"))}


def filtrar_escopo_lancamentos(qs, escopo: str):
    if escopo == "pessoal":
        return qs.exclude(CENTRO_EMPRESA)
    if escopo == "empresa":
        return qs.filter(CENTRO_EMPRESA)
    return qs


def filtrar_escopo_contas(qs, escopo: str):
    if escopo == "pessoal":
        return qs.filter(titular__tipo=Titular.PF)
    if escopo == "empresa":
        return qs.filter(Q(titular__tipo=Titular.PJ) | Q(centro_padrao__empresa=True))
    return qs


# ---------------------------------------------------------------------------
# Saldos e bloqueios
# ---------------------------------------------------------------------------


def saldos_contas(contas=None, ate: date | None = None) -> list[Conta]:
    """Calcula `saldo_atual` em cada conta respeitando `data_saldo_inicial` e lançamentos pagos."""
    ate = ate or hoje()
    if contas is None:
        contas = Conta.objects.filter(ativo=True).select_related("titular")
    contas = list(contas)
    ids = [c.id for c in contas]
    if not ids:
        return contas

    pagos = Lancamento.objects.filter(pago=True, data_pagamento__lte=ate)
    entradas = _por(
        pagos.filter(
            tipo__in=[Lancamento.RECEITA, Lancamento.ESTORNO],
            conta_id__in=ids,
            data_pagamento__gte=F("conta__data_saldo_inicial"),
        ),
        "conta_id",
    )
    recebidas = _por(
        pagos.filter(
            tipo=Lancamento.TRANSFERENCIA,
            conta_destino_id__in=ids,
            data_pagamento__gte=F("conta_destino__data_saldo_inicial"),
        ),
        "conta_destino_id",
    )
    saidas = _por(
        pagos.filter(
            tipo__in=[Lancamento.DESPESA, Lancamento.TRANSFERENCIA, Lancamento.PAGAMENTO_FATURA],
            conta_id__in=ids,
            data_pagamento__gte=F("conta__data_saldo_inicial"),
        ),
        "conta_id",
    )
    for c in contas:
        inicial = c.saldo_inicial if ate >= c.data_saldo_inicial else ZERO
        c.saldo_atual = inicial + entradas.get(c.id, ZERO) + recebidas.get(c.id, ZERO) - saidas.get(c.id, ZERO)
    return contas


def saldos_por_titular(contas: list[Conta]) -> list[dict[str, Any]]:
    grupos: OrderedDict[Titular, list[Conta]] = OrderedDict()
    for c in contas:
        grupos.setdefault(c.titular, []).append(c)
    return [
        {"titular": t, "contas": cs, "total": sum((c.saldo_atual for c in cs), ZERO)}
        for t, cs in grupos.items()
    ]


def verificar_bloqueio_lancamento(
    data_ref: date | None,
    conta: Conta | None = None,
    conta_destino: Conta | None = None,
) -> str | None:
    if not data_ref:
        return None
    mes_ref = inicio_mes(data_ref)
    if FechamentoMensal.objects.filter(mes=mes_ref, fechado=True).exists():
        return f"O mês {mes_ref:%m/%Y} está fechado. Reabra-o em Fechamento Mensal para alterar lançamentos."
    for c in (conta, conta_destino):
        if c is not None and c.bloqueado_ate and data_ref <= c.bloqueado_ate:
            return f"A conta “{c.nome}” está bloqueada para edições até {c.bloqueado_ate:%d/%m/%Y}."
    return None


# ---------------------------------------------------------------------------
# Mês, categorias, orçamento
# ---------------------------------------------------------------------------


def lancamentos_do_mes(ini: date):
    return Lancamento.objects.filter(data__gte=ini, data__lte=fim_mes(ini))


def despesas_por_categoria(qs):
    expr = Sum(
        Case(
            When(tipo=Lancamento.DESPESA, then=F("valor")),
            When(tipo=Lancamento.ESTORNO, then=-F("valor")),
            default=ZERO,
            output_field=DEC_OUT,
        )
    )
    return (
        qs.filter(tipo__in=[Lancamento.DESPESA, Lancamento.ESTORNO])
        .annotate(grupo=Coalesce("categoria__pai__nome", "categoria__nome", Value("Sem categoria"), output_field=CharField()))
        .order_by()
        .values("grupo")
        .annotate(t=expr)
        .filter(t__gt=ZERO)
        .order_by("-t")
    )


def orcamento_mes(ini: date) -> list[dict[str, Any]]:
    fim = fim_mes(ini)
    orcamentos = list(
        Orcamento.objects.filter(ativo=True, vigencia_inicio__lte=fim)
        .filter(Q(vigencia_fim__isnull=True) | Q(vigencia_fim__gte=ini))
        .select_related("categoria__pai", "centro")
    )
    if not orcamentos:
        return []

    gastos_brutos = list(
        lancamentos_do_mes(ini)
        .filter(tipo__in=[Lancamento.DESPESA, Lancamento.ESTORNO], categoria__isnull=False)
        .order_by()
        .values("categoria_id", "categoria__pai_id", "centro_id")
        .annotate(
            t=Sum(
                Case(
                    When(tipo=Lancamento.DESPESA, then=F("valor")),
                    When(tipo=Lancamento.ESTORNO, then=-F("valor")),
                    default=ZERO,
                    output_field=DEC_OUT,
                )
            )
        )
    )
    itens = []
    for o in orcamentos:
        cat_id = o.categoria_id
        gasto = sum(
            (
                g["t"] or ZERO
                for g in gastos_brutos
                if (g["categoria_id"] == cat_id or g["categoria__pai_id"] == cat_id)
                and (o.centro_id is None or g["centro_id"] == o.centro_id)
            ),
            ZERO,
        )
        gasto = max(gasto, ZERO)
        pct = int(gasto / o.valor_mensal * 100) if o.valor_mensal else 0
        restante = o.valor_mensal - gasto
        itens.append({
            "o": o,
            "gasto": gasto,
            "pct": pct,
            "restante": restante,
            "excesso": max(-restante, ZERO),
        })
    itens.sort(key=lambda i: (-i["pct"], i["o"].categoria.nome))
    return itens


def totais_orcamento(itens: list[dict[str, Any]]) -> tuple[Decimal, Decimal]:
    """Soma limites e gastos evitando dupla contagem entre categoria pai e subcategoria."""
    pais_orcados = {(i["o"].categoria_id, i["o"].centro_id) for i in itens if i["o"].categoria.pai_id is None}
    total_limite = ZERO
    total_gasto = ZERO
    for i in itens:
        o = i["o"]
        pai_id = o.categoria.pai_id
        if pai_id and ((pai_id, None) in pais_orcados or (pai_id, o.centro_id) in pais_orcados):
            continue
        total_limite += o.valor_mensal
        total_gasto += i["gasto"]
    return total_limite, total_gasto


# ---------------------------------------------------------------------------
# Contas a vencer, cartões e faturas
# ---------------------------------------------------------------------------


def contas_a_vencer(ate: date) -> tuple[list[Lancamento], list[Fatura]]:
    pendentes = (
        Lancamento.objects.filter(pago=False, vencimento__lte=ate)
        .exclude(tipo=Lancamento.PAGAMENTO_FATURA)
        .select_related("conta__titular", "conta_destino", "categoria__pai", "centro")
        .order_by("vencimento", "id")
    )
    faturas = [
        f
        for f in Fatura.objects.com_totais().filter(paga=False, vencimento__lte=ate).select_related("cartao").order_by("vencimento")
        if f.saldo > ZERO
    ]
    return list(pendentes), faturas


def resumo_cartoes() -> list[dict[str, Any]]:
    cartoes = list(Cartao.objects.filter(ativo=True).select_related("titular"))
    if not cartoes:
        return []
    h = hoje()
    refs_atuais = {c.id: c.referencia_para(h) for c in cartoes}
    faturas = list(
        Fatura.objects.com_totais()
        .filter(cartao__in=cartoes)
        .filter(Q(paga=False) | Q(referencia__in=list(refs_atuais.values())))
    )
    por_chave = {(f.cartao_id, f.referencia): f for f in faturas}
    usado_por_cartao: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for f in faturas:
        if not f.paga:
            usado_por_cartao[f.cartao_id] += f.saldo

    dados = []
    for c in cartoes:
        ref = refs_atuais[c.id]
        atual = por_chave.get((c.id, ref)) or c.fatura_virtual(ref)
        usado = max(usado_por_cartao[c.id], ZERO)
        dados.append({
            "cartao": c,
            "atual": atual,
            "usado": usado,
            "disponivel": (c.limite - usado) if c.limite is not None else None,
            "pct": int(usado / c.limite * 100) if c.limite else 0,
        })
    return dados


@transaction.atomic
def pagar_fatura(
    fatura_pk: int,
    *,
    conta: Conta,
    valor: Decimal,
    data: date,
    usuario: Any = None,
    request: Any = None,
) -> Lancamento:
    fatura = Fatura.objects.select_for_update().select_related("cartao").get(pk=fatura_pk)
    if not conta.ativo:
        raise ValidationError("A conta selecionada está inativa.")
    msg_bloqueio = verificar_bloqueio_lancamento(data, conta=conta)
    if msg_bloqueio:
        raise ValidationError(msg_bloqueio)

    fatura.atualizar_status()
    if fatura.saldo <= ZERO:
        raise ValidationError("Esta fatura já está quitada.")
    if valor <= ZERO:
        raise ValidationError("O valor pago deve ser maior que zero.")
    if valor > fatura.saldo:
        raise ValidationError(
            f"O valor informado (R$ {valor:.2f}) excede o saldo em aberto da fatura (R$ {fatura.saldo:.2f})."
        )

    lanc = Lancamento.objects.create(
        tipo=Lancamento.PAGAMENTO_FATURA,
        descricao=f"Pagamento da fatura {fatura}",
        valor=valor,
        data=data,
        vencimento=fatura.vencimento,
        pago=True,
        data_pagamento=data,
        conta=conta,
        fatura_paga=fatura,
        criado_por=usuario,
        atualizado_por=usuario,
    )
    registrar_auditoria(
        usuario,
        RegistroAuditoria.PAGAR,
        lanc,
        f"Pagamento de fatura: {fatura} (R$ {valor})",
        {"fatura_id": fatura.pk, "conta": str(conta), "valor": str(valor), "data": data.isoformat()},
        request=request,
    )
    return lanc


@transaction.atomic
def criar_parcelado(base: Lancamento, n: int) -> list[Lancamento]:
    """Divide uma compra no cartão em `n` parcelas, uma por fatura consecutiva."""
    n = int(n)
    parcela = (base.valor / n).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    resto = base.valor - parcela * n
    grupo = uuid.uuid4()
    ref_inicial = base.cartao.referencia_para(base.data)
    itens = []
    for k in range(n):
        fatura_k = base.cartao.fatura_por_referencia(somar_meses(ref_inicial, k, dia=1))
        item = Lancamento(
            tipo=base.tipo,
            descricao=base.descricao,
            valor=parcela + (resto if k == 0 else ZERO),
            data=somar_meses(base.data, k),
            cartao=base.cartao,
            fatura=fatura_k,
            categoria=base.categoria,
            centro=base.centro,
            veiculo=base.veiculo,
            compartilhado=base.compartilhado,
            pago_por=base.pago_por,
            observacao=base.observacao,
            criado_por=base.criado_por,
            atualizado_por=base.atualizado_por or base.criado_por,
            grupo_parcelas=grupo,
            parcela=k + 1,
            total_parcelas=n,
            pago=True,
        )
        item.save(manter_fatura=True)
        itens.append(item)
    return itens


def recalcular_todas_faturas() -> int:
    alteradas = 0
    for f in Fatura.objects.all():
        antes = f.paga
        f.atualizar_status()
        if f.paga != antes:
            alteradas += 1
    return alteradas


# ---------------------------------------------------------------------------
# Recorrências
# ---------------------------------------------------------------------------


def horizonte_recorrencias() -> date:
    return hoje() + timedelta(days=45)


def gerar_recorrencias(ate: date | None = None, forcar: bool = False) -> int:
    ate_efetivo = ate or horizonte_recorrencias()
    chave = _cache_key_recorrencias()
    if not forcar and ate is None and cache.get(chave):
        return 0
    total = 0
    for pk in Recorrencia.objects.filter(ativo=True).values_list("pk", flat=True):
        with transaction.atomic():
            r = Recorrencia.objects.select_for_update().select_related("conta", "cartao").get(pk=pk)
            total += r.gerar(ate_efetivo)
    if ate is None:
        cache.set(chave, True, timeout=3600 * 6)
    return total


@transaction.atomic
def sincronizar_recorrencia(r: Recorrencia) -> None:
    """Aplica a edição de uma recorrência aos lançamentos futuros ainda editáveis."""
    h = hoje()
    futuros = Lancamento.objects.filter(recorrencia=r, data__gte=h).filter(
        Q(pago=False) | Q(cartao__isnull=False, fatura__paga=False)
    )
    if not r.ativo:
        for item in futuros:
            item.delete()
    else:
        if r.fim:
            for item in futuros.filter(data__gt=r.fim):
                item.delete()
        restantes = futuros.filter(data__lte=r.fim) if r.fim else futuros
        for item in restantes:
            nova_data = item.data.replace(day=dia_seguro(item.data.year, item.data.month, r.dia))
            item.data = nova_data
            item.vencimento = nova_data
            item.descricao, item.valor, item.tipo = r.descricao, r.valor, r.tipo
            item.categoria, item.centro, item.veiculo = r.categoria, r.centro, r.veiculo
            item.conta, item.cartao = r.conta, r.cartao
            item.pago = bool(r.cartao_id)
            item.atualizado_por = r.atualizado_por
            item.save()
    r.ultima_gerada = Lancamento.objects.filter(recorrencia=r).aggregate(m=Max("data"))["m"]
    r.save(update_fields=["ultima_gerada"])
    cache.delete(_cache_key_recorrencias())


# ---------------------------------------------------------------------------
# Veículo
# ---------------------------------------------------------------------------

_CODIGOS_CATEGORIA_VEICULO = {
    "combustível": "combustivel",
    "combustivel": "combustivel",
    "manutenção": "manutencao",
    "manutencao": "manutencao",
}


def centro_veiculo() -> CentroCusto | None:
    return (
        CentroCusto.objects.filter(codigo_sistema="veiculo").first()
        or CentroCusto.objects.filter(nome__iexact="Veículo").first()
    )


def categoria_veiculo(nome: str) -> Categoria:
    codigo = _CODIGOS_CATEGORIA_VEICULO.get(nome.strip().lower())
    if codigo:
        cat = Categoria.objects.filter(tipo="D", codigo_sistema=codigo).first()
        if cat:
            return cat
    pai = (
        Categoria.objects.filter(tipo="D", codigo_sistema="veiculo", pai__isnull=True).first()
        or Categoria.objects.filter(tipo="D", nome__iexact="Veículo", pai__isnull=True).first()
    )
    if pai:
        cat = Categoria.objects.filter(tipo="D", pai=pai, nome__iexact=nome).first()
        if cat:
            return cat
    cat = Categoria.objects.filter(tipo="D", nome__iexact=nome).first()
    if cat:
        return cat
    if pai is None:
        pai = Categoria.objects.create(nome="Veículo", tipo="D", pai=None, codigo_sistema="veiculo")
    return Categoria.objects.create(nome=nome, tipo="D", pai=pai, codigo_sistema=codigo)


def sincronizar_lancamento_veiculo(
    obj,
    descricao: str,
    valor: Decimal | None,
    categoria_nome: str,
    conta: Conta | None = None,
    cartao: Cartao | None = None,
    usuario: Any = None,
) -> None:
    """Mantém o lançamento financeiro ligado a um abastecimento ou manutenção."""
    lanc = obj.lancamento
    if valor and valor > 0:
        if lanc is None:
            lanc = Lancamento(
                tipo=Lancamento.DESPESA,
                categoria=categoria_veiculo(categoria_nome),
                centro=obj.veiculo.centro or centro_veiculo(),
                veiculo=obj.veiculo,
                pago=True,
                criado_por=usuario,
            )
        lanc.descricao = descricao
        lanc.valor = valor
        lanc.data = obj.data
        lanc.vencimento = obj.data
        lanc.conta = conta
        lanc.cartao = cartao
        lanc.pago = True
        lanc.data_pagamento = obj.data if not cartao else lanc.data_pagamento
        lanc.atualizado_por = usuario
        lanc.save()
        if obj.lancamento_id != lanc.pk:
            obj.lancamento = lanc
            obj.save(update_fields=["lancamento"])
    elif lanc is not None:
        obj.lancamento = None
        obj.save(update_fields=["lancamento"])
        lanc.delete()


def consumo_abastecimentos(veiculo: Veiculo):
    """Calcula km/l entre tanques cheios. Retorna (lista mais recente primeiro, média)."""
    itens = list(veiculo.abastecimentos.order_by("km", "data", "id"))
    ultimo_cheio, litros = None, ZERO
    total_km, total_l = 0, ZERO
    for a in itens:
        a.km_l = None
        if ultimo_cheio is not None:
            litros += a.litros
            if a.tanque_cheio:
                distancia = a.km - ultimo_cheio
                if litros > 0 and distancia > 0:
                    a.km_l = (Decimal(distancia) / litros).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    total_km += distancia
                    total_l += litros
                ultimo_cheio, litros = a.km, ZERO
        elif a.tanque_cheio:
            ultimo_cheio, litros = a.km, ZERO
    media = (Decimal(total_km) / total_l).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if total_l else None
    itens.reverse()
    return itens, media


def alertas_manutencao(veiculo: Veiculo, km_aviso: int = 500, dias_aviso: int = 30) -> list[dict[str, Any]]:
    km_atual = veiculo.km_atual()
    h = hoje()
    vistos, alertas = set(), []
    for m in veiculo.manutencoes.order_by("-data", "-id"):
        chave = m.servico.strip().lower()
        if chave in vistos:
            continue
        vistos.add(chave)
        motivos, vencido = [], False
        if m.proxima_km:
            falta = m.proxima_km - km_atual
            if falta <= km_aviso:
                vencido |= falta < 0
                motivos.append(f"passou {abs(falta)} km" if falta < 0 else f"faltam {falta} km")
        if m.proxima_data:
            dias = (m.proxima_data - h).days
            if dias <= dias_aviso:
                vencido |= dias < 0
                motivos.append(f"venceu em {m.proxima_data:%d/%m/%Y}" if dias < 0 else f"até {m.proxima_data:%d/%m/%Y}")
        if motivos:
            alertas.append({"manutencao": m, "texto": ", ".join(motivos), "vencido": vencido})
    return alertas


def resumo_veiculo(veiculo: Veiculo, ano: int) -> dict[str, Any]:
    lancs = Lancamento.objects.filter(veiculo=veiculo, tipo__in=[Lancamento.DESPESA, Lancamento.ESTORNO], data__year=ano)
    total = despesas_liquidas(lancs)
    expr = Sum(
        Case(
            When(tipo=Lancamento.DESPESA, then=F("valor")),
            When(tipo=Lancamento.ESTORNO, then=-F("valor")),
            default=ZERO,
            output_field=DEC_OUT,
        )
    )
    por_categoria = (
        lancs.annotate(nome=Coalesce("categoria__nome", Value("Sem categoria"), output_field=CharField()))
        .order_by()
        .values("nome")
        .annotate(t=expr)
        .filter(t__gt=ZERO)
        .order_by("-t")
    )
    kms = veiculo.abastecimentos.filter(data__year=ano).aggregate(mn=Min("km"), mx=Max("km"))
    km_base = veiculo.abastecimentos.filter(data__year__lt=ano).aggregate(mx=Max("km"))["mx"]
    if kms["mx"] is not None:
        inicio_km = km_base if km_base is not None else kms["mn"]
        rodado = max(0, kms["mx"] - inicio_km)
    else:
        rodado = 0
    ini = hoje().replace(day=1)
    return {
        "total": total,
        "por_categoria": list(por_categoria),
        "rodado": rodado,
        "custo_km": (total / rodado).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if rodado else None,
        "combustivel_mes": soma(veiculo.abastecimentos.filter(data__gte=ini, data__lte=fim_mes(ini)), "valor_total"),
    }


# ---------------------------------------------------------------------------
# Relatórios, Fluxo Projetado, Rateio e Metas
# ---------------------------------------------------------------------------


def resumo_empresa(centro: CentroCusto, ini: date) -> dict[str, Any]:
    inicio = somar_meses(ini, -11, dia=1)
    fim = fim_mes(ini)
    base = Lancamento.objects.filter(centro=centro, data__gte=inicio, data__lte=fim)
    entradas = por_mes(base.filter(tipo=Lancamento.RECEITA))
    impostos = por_mes(base.filter(tipo__in=[Lancamento.DESPESA, Lancamento.ESTORNO]).filter(IMPOSTO), liquido_despesa=True)
    saidas = por_mes(base.filter(tipo__in=[Lancamento.DESPESA, Lancamento.ESTORNO]).exclude(IMPOSTO), liquido_despesa=True)
    contas_empresa = Conta.objects.filter(centro_padrao=centro)
    retiradas = por_mes(
        Lancamento.objects.filter(tipo=Lancamento.TRANSFERENCIA, conta__in=contas_empresa, data__gte=inicio, data__lte=fim)
        .exclude(conta_destino__in=contas_empresa)
    )
    aportes = por_mes(
        Lancamento.objects.filter(tipo=Lancamento.TRANSFERENCIA, conta_destino__in=contas_empresa, data__gte=inicio, data__lte=fim)
        .exclude(conta__in=contas_empresa)
    )
    meses = []
    for k in range(12):
        m = somar_meses(inicio, k, dia=1)
        e, s, i = entradas.get(m, ZERO), saidas.get(m, ZERO), impostos.get(m, ZERO)
        meses.append({
            "mes": m,
            "entradas": e,
            "saidas": s,
            "impostos": i,
            "resultado": e - s - i,
            "retiradas": retiradas.get(m, ZERO),
            "aportes": aportes.get(m, ZERO),
        })
    totais = {k: sum((m[k] for m in meses), ZERO) for k in ("entradas", "saidas", "impostos", "resultado", "retiradas", "aportes")}
    do_mes = (
        Lancamento.objects.filter(centro=centro, data__gte=ini, data__lte=fim)
        .exclude(tipo=Lancamento.TRANSFERENCIA)
        .select_related("categoria__pai", "conta", "cartao", "criado_por")
        .order_by("data", "id")
    )
    impostos_ano = (
        Lancamento.objects.filter(centro=centro, tipo__in=[Lancamento.DESPESA, Lancamento.ESTORNO], data__year=ini.year)
        .filter(IMPOSTO)
        .select_related("categoria__pai", "conta")
        .order_by("data")
    )
    return {
        "meses": meses,
        "totais": totais,
        "atual": meses[-1],
        "do_mes": do_mes,
        "impostos_ano": impostos_ano,
        "total_impostos_ano": despesas_liquidas(impostos_ano),
        "contas": saldos_contas(contas_empresa.select_related("titular")),
    }


def resumo_anual(ano: int, escopo: str = "todos") -> dict[str, Any]:
    base = filtrar_escopo_lancamentos(Lancamento.objects.filter(data__year=ano), escopo)
    receitas = por_mes(base.filter(tipo=Lancamento.RECEITA))
    despesas = por_mes(base.filter(tipo__in=[Lancamento.DESPESA, Lancamento.ESTORNO]), liquido_despesa=True)
    meses = []
    for m in range(1, 13):
        d = date(ano, m, 1)
        r, dsp = receitas.get(d, ZERO), despesas.get(d, ZERO)
        meses.append({"mes": d, "receitas": r, "despesas": dsp, "resultado": r - dsp})

    expr_centro = Sum(
        Case(
            When(tipo=Lancamento.ESTORNO, then=-F("valor")),
            default=F("valor"),
            output_field=DEC_OUT,
        )
    )
    centros = base.filter(tipo__in=[Lancamento.RECEITA, Lancamento.DESPESA, Lancamento.ESTORNO]).order_by().values(
        "centro__nome", "tipo"
    ).annotate(t=expr_centro)
    por_centro: OrderedDict[str, dict[str, Decimal]] = OrderedDict()
    for c in centros:
        nome = c["centro__nome"] or "Sem centro"
        slot = por_centro.setdefault(nome, {"R": ZERO, "D": ZERO})
        if c["tipo"] == Lancamento.RECEITA:
            slot["R"] += c["t"] or ZERO
        else:
            slot["D"] += c["t"] or ZERO
    total_receitas = sum((m["receitas"] for m in meses), ZERO)
    total_despesas = sum((m["despesas"] for m in meses), ZERO)
    return {
        "meses": meses,
        "total_receitas": total_receitas,
        "total_despesas": total_despesas,
        "total_resultado": total_receitas - total_despesas,
        "por_centro": [
            {"nome": k, "receitas": v["R"], "despesas": v["D"], "resultado": v["R"] - v["D"]}
            for k, v in por_centro.items()
        ],
        "categorias": despesas_por_categoria(base),
    }


def fluxo_caixa_projetado(dias: int = 60, conta_id: int | None = None, escopo: str = "todos") -> dict[str, Any]:
    h = hoje()
    fim = h + timedelta(days=dias)
    contas_qs = filtrar_escopo_contas(Conta.objects.filter(ativo=True).select_related("titular"), escopo)
    if conta_id:
        contas_qs = contas_qs.filter(pk=conta_id)
    contas = saldos_contas(contas_qs, ate=h)
    ids_contas = {c.id for c in contas}
    saldo_inicial = sum((c.saldo_atual for c in contas), ZERO)

    eventos: list[dict[str, Any]] = []
    delta_por_dia: dict[date, Decimal] = defaultdict(lambda: ZERO)

    def _adicionar_evento(data_ev: date, descricao: str, valor_signed: Decimal, origem: str, conta_nome: str = "", atrasado: bool = False):
        data_caixa = h if data_ev < h else data_ev
        delta_por_dia[data_caixa] += valor_signed
        eventos.append({
            "data": data_ev,
            "data_caixa": data_caixa,
            "descricao": descricao,
            "valor": valor_signed,
            "origem": origem,
            "conta": conta_nome,
            "atrasado": atrasado or data_ev < h,
        })

    # 1. Lançamentos pendentes em conta
    pendentes = (
        Lancamento.objects.filter(pago=False, vencimento__lte=fim, cartao__isnull=True)
        .exclude(tipo=Lancamento.PAGAMENTO_FATURA)
        .select_related("conta", "conta_destino")
    )
    for l in pendentes:
        data_ev = l.vencimento or l.data
        if l.tipo == Lancamento.RECEITA and l.conta_id in ids_contas:
            _adicionar_evento(data_ev, l.descricao, l.valor, "A receber", l.conta.nome)
        elif l.tipo == Lancamento.ESTORNO and l.conta_id in ids_contas:
            _adicionar_evento(data_ev, l.descricao, l.valor, "Estorno previsto", l.conta.nome)
        elif l.tipo == Lancamento.DESPESA and l.conta_id in ids_contas:
            _adicionar_evento(data_ev, l.descricao, -l.valor, "A pagar", l.conta.nome)
        elif l.tipo == Lancamento.TRANSFERENCIA:
            sai = l.conta_id in ids_contas
            entra = l.conta_destino_id in ids_contas
            if sai and not entra:
                _adicionar_evento(data_ev, f"{l.descricao} (saída)", -l.valor, "Transferência", l.conta.nome)
            elif entra and not sai:
                _adicionar_evento(data_ev, f"{l.descricao} (entrada)", l.valor, "Transferência", l.conta_destino.nome)

    # 2. Faturas em aberto
    faturas_abertas = Fatura.objects.com_totais().filter(paga=False, vencimento__lte=fim).select_related(
        "cartao__conta_pagamento", "cartao__titular"
    )
    for f in faturas_abertas:
        if f.saldo <= ZERO:
            continue
        conta_pg = f.cartao.conta_pagamento
        if conta_id and (not conta_pg or conta_pg.id != conta_id):
            continue
        if not conta_id and conta_pg and conta_pg.id not in ids_contas:
            continue
        if not conta_id and not conta_pg:
            if escopo == "pessoal" and f.cartao.titular.tipo != Titular.PF:
                continue
            if escopo == "empresa" and f.cartao.titular.tipo != Titular.PJ:
                continue
        _adicionar_evento(
            f.vencimento,
            f"Fatura {f.cartao.nome} ({f.referencia:%m/%Y})",
            -f.saldo,
            "Fatura cartão",
            conta_pg.nome if conta_pg else "Sem conta vinculada",
        )

    # 3. Recorrências futuras ainda não materializadas
    for r in Recorrencia.objects.filter(ativo=True).select_related("conta", "cartao__conta_pagamento", "cartao__titular"):
        existentes = set(r.lancamentos.values_list("data__year", "data__month"))
        for d in r.datas(fim):
            if d < h or (d.year, d.month) in existentes:
                continue
            if r.conta_id:
                if r.conta_id not in ids_contas:
                    continue
                v = r.valor if r.tipo == Lancamento.RECEITA else -r.valor
                _adicionar_evento(d, f"{r.descricao} (recorrente)", v, "Recorrência", r.conta.nome)
            elif r.cartao_id:
                ref = r.cartao.referencia_para(d)
                fat_v = r.cartao.fatura_virtual(ref)
                if fat_v.vencimento > fim:
                    continue
                conta_pg = r.cartao.conta_pagamento
                if conta_id and (not conta_pg or conta_pg.id != conta_id):
                    continue
                if not conta_id and conta_pg and conta_pg.id not in ids_contas:
                    continue
                if not conta_id and not conta_pg:
                    if escopo == "pessoal" and r.cartao.titular.tipo != Titular.PF:
                        continue
                    if escopo == "empresa" and r.cartao.titular.tipo != Titular.PJ:
                        continue
                v = r.valor if r.tipo == Lancamento.RECEITA else -r.valor
                _adicionar_evento(
                    fat_v.vencimento,
                    f"{r.descricao} (recorrente cartão)",
                    v,
                    "Recorrência cartão",
                    conta_pg.nome if conta_pg else r.cartao.nome,
                )

    eventos.sort(key=lambda e: (e["data_caixa"], e["data"], e["descricao"]))
    serie = []
    saldo = saldo_inicial
    saldo_minimo = saldo_inicial
    data_minimo = h
    for i in range(dias + 1):
        dia = h + timedelta(days=i)
        saldo += delta_por_dia.get(dia, ZERO)
        if saldo < saldo_minimo:
            saldo_minimo = saldo
            data_minimo = dia
        serie.append({"data": dia, "saldo": saldo})

    return {
        "dias": dias,
        "saldo_inicial": saldo_inicial,
        "saldo_final": saldo,
        "saldo_minimo": saldo_minimo,
        "data_minimo": data_minimo,
        "fica_negativo": saldo_minimo < ZERO,
        "serie": serie,
        "eventos": eventos,
    }


def rateio_mensal(ini: date, modo: str = "igual", incluir_casa: bool = True) -> dict[str, Any]:
    """Calcula o rateio de despesas compartilhadas entre titulares PF no mês."""
    titulares_pf = list(Titular.objects.filter(ativo=True, tipo=Titular.PF).order_by("nome"))
    qs = lancamentos_do_mes(ini).filter(tipo__in=[Lancamento.DESPESA, Lancamento.ESTORNO])
    if incluir_casa:
        qs = qs.filter(Q(compartilhado=True) | Q(centro__codigo_sistema="casa") | Q(centro__nome__iexact="Casa"))
    else:
        qs = qs.filter(compartilhado=True)
    qs = qs.select_related("pago_por", "conta__titular", "cartao__titular", "categoria__pai", "centro")

    itens = list(qs)
    pago_por_titular: dict[int, Decimal] = {t.id: ZERO for t in titulares_pf}
    total = ZERO
    nao_atribuido = ZERO
    for l in itens:
        val = l.valor if l.tipo == Lancamento.DESPESA else -l.valor
        total += val
        tit = l.pago_por or (l.cartao.titular if l.cartao_id else (l.conta.titular if l.conta_id else None))
        if tit and tit.id in pago_por_titular:
            pago_por_titular[tit.id] += val
        else:
            nao_atribuido += val

    # Receitas PF no mês para modo proporcional
    receitas_pf = {t.id: ZERO for t in titulares_pf}
    for r in (
        lancamentos_do_mes(ini)
        .filter(tipo=Lancamento.RECEITA, conta__titular__tipo=Titular.PF)
        .values("conta__titular_id")
        .annotate(t=Sum("valor"))
    ):
        if r["conta__titular_id"] in receitas_pf:
            receitas_pf[r["conta__titular_id"]] = r["t"] or ZERO

    total_receitas_pf = sum(receitas_pf.values(), ZERO)
    n = len(titulares_pf) or 1
    participantes = []
    for t in titulares_pf:
        if modo == "renda" and total_receitas_pf > ZERO:
            frac = receitas_pf[t.id] / total_receitas_pf
        else:
            frac = Decimal("1") / Decimal(n)
        cota = (total * frac).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        pago = pago_por_titular[t.id]
        saldo = pago - cota  # positivo = tem a receber, negativo = deve pagar
        participantes.append({
            "titular": t,
            "receita": receitas_pf[t.id],
            "pct": (frac * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP),
            "pago": pago,
            "cota": cota,
            "saldo": saldo,
        })

    # Calcula transferências sugeridas (credores x devedores)
    devedores = [{"t": p["titular"], "val": -p["saldo"]} for p in participantes if p["saldo"] < ZERO]
    credores = [{"t": p["titular"], "val": p["saldo"]} for p in participantes if p["saldo"] > ZERO]
    acertos = []
    i, j = 0, 0
    while i < len(devedores) and j < len(credores):
        d, c = devedores[i], credores[j]
        v = min(d["val"], c["val"])
        if v >= Decimal("0.01"):
            acertos.append({"de": d["t"], "para": c["t"], "valor": v.quantize(Decimal("0.01"))})
        d["val"] -= v
        c["val"] -= v
        if d["val"] < Decimal("0.01"):
            i += 1
        if c["val"] < Decimal("0.01"):
            j += 1

    return {
        "total": total,
        "nao_atribuido": nao_atribuido,
        "participantes": participantes,
        "acertos": acertos,
        "itens": itens,
        "modo": modo,
        "incluir_casa": incluir_casa,
    }


def resumo_metas() -> list[dict[str, Any]]:
    metas = list(MetaFinanceira.objects.filter(ativo=True).select_related("conta__titular"))
    contas_ids = [m.conta for m in metas if m.conta_id]
    saldos_map = {}
    if contas_ids:
        for c in saldos_contas(contas_ids):
            saldos_map[c.id] = c.saldo_atual
    resultado = []
    for m in metas:
        atual = m.valor_inicial + (saldos_map.get(m.conta_id, ZERO) if m.conta_id else ZERO)
        pct = int(atual / m.valor_alvo * 100) if m.valor_alvo else 0
        falta = max(m.valor_alvo - atual, ZERO)
        resultado.append({"m": m, "atual": atual, "pct": pct, "falta": falta})
    return resultado
