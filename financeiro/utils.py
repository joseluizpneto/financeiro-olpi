from __future__ import annotations

import calendar
from datetime import date, timedelta

from django.utils import timezone

ANO_MIN = 1900
ANO_MAX = 2100


def hoje() -> date:
    return timezone.localdate()


def dia_seguro(ano: int, mes: int, dia: int) -> int:
    """Limita o dia ao último dia do mês (ex.: dia 31 em fevereiro vira 28/29)."""
    return min(max(int(dia), 1), calendar.monthrange(ano, mes)[1])


def ano_valido(ano: int) -> bool:
    return ANO_MIN <= ano <= ANO_MAX


def somar_meses(d: date, n: int, dia: int | None = None) -> date:
    total = d.year * 12 + (d.month - 1) + n
    ano, mes = divmod(total, 12)
    mes += 1
    ano = min(max(ano, ANO_MIN), ANO_MAX)
    return date(ano, mes, dia_seguro(ano, mes, dia if dia is not None else d.day))


def inicio_mes(d: date) -> date:
    return d.replace(day=1)


def fim_mes(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def intervalo_ciclo(ref: date, dia_inicio: int = 1, referencia: str = "seguinte") -> tuple[date, date]:
    """Retorna (data_inicio, data_fim) do ciclo financeiro correspondente ao mês `ref`."""
    ref = ref.replace(day=1)
    dia = min(max(int(dia_inicio or 1), 1), 31)
    if dia == 1:
        return ref, fim_mes(ref)
    if referencia == "atual":
        ini = somar_meses(ref, 0, dia=dia)
        prox = somar_meses(ref, 1, dia=dia)
    else:
        ini = somar_meses(ref, -1, dia=dia)
        prox = somar_meses(ref, 0, dia=dia)
    return ini, prox - timedelta(days=1)


def mes_referencia_ciclo(d: date, dia_inicio: int = 1, referencia: str = "seguinte") -> date:
    """Retorna o 1º dia do mês de referência ao qual a data `d` pertence no ciclo."""
    dia = min(max(int(dia_inicio or 1), 1), 31)
    base = d.replace(day=1)
    if dia == 1:
        return base
    corte = dia_seguro(d.year, d.month, dia)
    if referencia == "atual":
        return base if d.day >= corte else somar_meses(base, -1, dia=1)
    return somar_meses(base, 1, dia=1) if d.day >= corte else base


def parse_mes(
    valor: str | None,
    padrao: date | None = None,
    *,
    dia_inicio: int = 1,
    referencia: str = "seguinte",
) -> date:
    """Converte 'AAAA-MM' no primeiro dia do mês; se inválido, usa o mês de referência atual."""
    if valor:
        try:
            ano_s, mes_s = valor.split("-")[:2]
            ano, mes = int(ano_s), int(mes_s)
            if ano_valido(ano) and 1 <= mes <= 12:
                return date(ano, mes, 1)
        except (ValueError, TypeError):
            pass
    return mes_referencia_ciclo(padrao or hoje(), dia_inicio=dia_inicio, referencia=referencia)


def mes_str(d: date) -> str:
    return d.strftime("%Y-%m")


def parse_data(valor: str | None) -> date | None:
    """Converte 'AAAA-MM-DD' em `date`; retorna None se vazio ou inválido."""
    if not valor:
        return None
    try:
        d = date.fromisoformat(valor.strip())
    except (ValueError, TypeError, AttributeError):
        return None
    return d if ano_valido(d.year) else None
