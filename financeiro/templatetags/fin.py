from decimal import Decimal, InvalidOperation

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def brl(valor):
    if valor is None or valor == "":
        return "—"
    try:
        v = Decimal(valor).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return valor
    texto = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"-R$ {texto}" if v < 0 else f"R$ {texto}"


@register.filter
def num(valor, casas=2):
    if valor is None:
        return "—"
    texto = f"{Decimal(valor):,.{int(casas)}f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


@register.filter
def neg(valor):
    """Classe CSS para valores negativos."""
    try:
        return "text-danger" if valor is not None and valor < 0 else ""
    except TypeError:
        return ""


@register.filter
def cor_tipo(tipo):
    return {
        "R": "text-success",
        "D": "text-danger",
        "E": "text-success",
        "T": "text-primary",
        "F": "text-body-secondary",
    }.get(tipo, "")


@register.filter
def sinal_tipo(tipo):
    return {"R": "+", "E": "+", "D": "−"}.get(tipo, "")


@register.filter
def limitar(valor, maximo=100):
    try:
        return max(0, min(int(valor), int(maximo)))
    except (TypeError, ValueError):
        return 0


@register.filter
def attr(obj, nome):
    if nome == "__str__":
        return str(obj)
    exibir = getattr(obj, f"get_{nome}_display", None)
    if callable(exibir):
        return exibir()
    v = getattr(obj, nome, "")
    if isinstance(v, bool):
        return (
            mark_safe('<i class="bi bi-check-circle-fill text-success"></i>')
            if v
            else mark_safe('<span class="text-body-secondary">—</span>')
        )
    if isinstance(v, Decimal):
        return format_html('<span class="{}">{}</span>', neg(v), brl(v))
    if v is None or v == "":
        return "—"
    return v


@register.simple_tag(takes_context=True)
def ativo(context, *prefixos):
    req = context.get("request")
    nome = getattr(getattr(req, "resolver_match", None), "url_name", "") or ""
    return "active" if any(nome.startswith(p) for p in prefixos) else ""


@register.inclusion_tag("financeiro/_situacao.html")
def situacao(lanc):
    return {"l": lanc}
