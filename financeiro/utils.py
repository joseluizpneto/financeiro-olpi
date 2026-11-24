from __future__ import annotations

import calendar
from datetime import date

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


def parse_mes(valor: str | None, padrao: date | None = None) -> date:
    """Converte 'AAAA-MM' no primeiro dia do mês; se inválido, usa o mês atual."""
    if valor:
        try:
            ano_s, mes_s = valor.split("-")[:2]
            ano, mes = int(ano_s), int(mes_s)
            if ano_valido(ano) and 1 <= mes <= 12:
                return date(ano, mes, 1)
        except (ValueError, TypeError):
            pass
    return inicio_mes(padrao or hoje())


def mes_str(d: date) -> str:
    return d.strftime("%Y-%m")
