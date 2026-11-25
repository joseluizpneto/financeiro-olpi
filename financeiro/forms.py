from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.validators import MinValueValidator
from django.db.models import F, Q
from django.db.models.functions import Coalesce

from .models import (
    Abastecimento, Cartao, Categoria, CentroCusto, ConfiguracaoSistema, Conta, FechamentoMensal,
    Lancamento, Manutencao, MetaFinanceira, Orcamento, Recorrencia, RegraCategorizacao,
    Titular, Veiculo,
)
from .security import validar_comprovante

User = get_user_model()
FORMAS = Lancamento.FORMAS_PAGAMENTO
FORMAS_IMPORTACAO = [("conta", "Conta bancária"), ("cartao", "Cartão de crédito")]


def _forma_inicial(obj) -> str:
    if getattr(obj, "forma_pagamento", ""):
        return obj.forma_pagamento
    if getattr(obj, "cartao_id", None):
        return Lancamento.FORMA_CREDITO
    if getattr(obj, "conta_id", None) and obj.conta.tipo == "carteira":
        return Lancamento.FORMA_DINHEIRO
    return Lancamento.FORMA_PIX


def ativos(qs, *atuais):
    ids = [i for i in atuais if i]
    return qs.filter(Q(ativo=True) | Q(pk__in=ids))


def categorias_qs():
    return Categoria.objects.select_related("pai").order_by(
        "tipo", Coalesce("pai__nome", "nome"), F("pai").asc(nulls_first=True), "nome"
    )


class SelectComTipo(forms.Select):
    """Select de categorias com data-tipo em cada opção (o JS filtra pelo tipo)."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        opcao = super().create_option(name, value, label, selected, index, subindex, attrs)
        instancia = getattr(value, "instance", None)
        if instancia is not None:
            opcao["attrs"]["data-tipo"] = instancia.tipo
        return opcao


class SelectContaComTipo(forms.Select):
    """Select de contas com data-tipo-conta em cada opção (o JS separa banco vs carteira)."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        opcao = super().create_option(name, value, label, selected, index, subindex, attrs)
        instancia = getattr(value, "instance", None)
        if instancia is not None:
            opcao["attrs"]["data-tipo-conta"] = instancia.tipo
        return opcao


_RE_BRL = re.compile(r"^[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?$")
_RE_DEC = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_RE_MILHAR = re.compile(r"^[+-]?\d{1,3}(?:\.\d{3})+$")


def normalizar_dinheiro_brl(valor):
    """Converte entrada em reais (ex.: 'R$ 1.234,56') para decimal com ponto, preservando formatos padrão."""
    if valor is None or isinstance(valor, (Decimal, int, float)):
        return valor
    s = str(valor).strip().replace("\u00a0", " ").replace("\u2212", "-")
    if not s:
        return ""
    if s[:2].lower() == "r$":
        s = s[2:].strip()
    elif s[0] in ("+", "-") and s[1:].lstrip()[:2].lower() == "r$":
        sinal = s[0]
        resto = s[1:].lstrip()[2:].strip()
        if resto.startswith(("+", "-")):
            return valor
        s = f"{sinal}{resto}"
    if s.startswith(("+", "-")):
        s = s[0] + s[1:].lstrip()
    if "," in s:
        if _RE_BRL.match(s):
            return s.replace(".", "").replace(",", ".")
        return valor
    if _RE_DEC.match(s):
        return s
    if s.count(".") >= 2 and _RE_MILHAR.match(s):
        return s.replace(".", "")
    return valor


def formatar_dinheiro_brl(valor) -> str:
    """Formata um valor numérico ou string monetária como '1.234,56' sem o prefixo 'R$'."""
    if valor is None or valor == "":
        return ""
    norm = normalizar_dinheiro_brl(valor)
    try:
        d = Decimal(str(norm)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return str(valor)
    texto = f"{abs(d):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"-{texto}" if d < 0 else texto


def _campo_nao_negativo(campo: forms.DecimalField, nome: str = "", form: forms.BaseForm | None = None) -> bool:
    if campo.min_value is not None and campo.min_value >= 0:
        return True
    validators = list(campo.validators)
    modelo = getattr(getattr(form, "_meta", None), "model", None)
    if modelo is not None and nome:
        try:
            validators.extend(modelo._meta.get_field(nome).validators)
        except Exception:
            pass
    for v in validators:
        if isinstance(v, MinValueValidator):
            lim = v.limit_value() if callable(v.limit_value) else v.limit_value
            if lim >= 0:
                return True
    return False


class DinheiroInput(forms.TextInput):
    """Input de texto mascarado para valores em Reais com prefixo visual 'R$'."""

    moeda_brl = True

    def format_value(self, value):
        return formatar_dinheiro_brl(value)

    def value_from_datadict(self, data, files, name):
        return normalizar_dinheiro_brl(super().value_from_datadict(data, files, name))


class EstiloMixin:
    def estilizar(self) -> None:
        for nome, campo in self.fields.items():
            if isinstance(campo, forms.DateField):
                campo.widget = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
                campo.widget.is_required = campo.required
            w = campo.widget
            if isinstance(campo, forms.DecimalField) and campo.decimal_places == 2:
                aceita_negativo = not _campo_nao_negativo(campo, nome, self)
                attrs = {k: v for k, v in w.attrs.items() if k not in ("type", "step")}
                classes = list(dict.fromkeys(f"{attrs.get('class', '')} form-control tabular".split()))
                attrs["class"] = " ".join(classes)
                attrs["inputmode"] = "text" if aceita_negativo else "numeric"
                attrs.setdefault("autocomplete", "off")
                attrs.setdefault("placeholder", "0,00")
                attrs["data-moeda"] = "brl"
                attrs["data-moeda-negativo"] = "1" if aceita_negativo else "0"
                if campo.max_digits is not None:
                    attrs["data-moeda-max-digitos"] = str(campo.max_digits)
                campo.widget = DinheiroInput(attrs=attrs)
                campo.widget.is_required = campo.required
                continue
            if isinstance(w, (forms.CheckboxInput, forms.RadioSelect)):
                classe = "form-check-input"
            elif isinstance(w, forms.Select):
                classe = "form-select"
            else:
                classe = "form-control"
            w.attrs["class"] = f"{w.attrs.get('class', '')} {classe}".strip()
            if isinstance(campo, forms.DecimalField):
                w.attrs.setdefault("inputmode", "decimal")
                w.attrs.setdefault("step", "0.01")


class MeioPagamentoMixin:
    """Campos de forma de pagamento (PIX, Débito, Crédito, Dinheiro) para formulários."""

    def adicionar_pagamento(
        self,
        conta_id: int | None = None,
        cartao_id: int | None = None,
        forma_pagamento: str = "",
        conta_tipo: str = "",
    ) -> None:
        self.fields["forma"] = forms.ChoiceField(
            choices=FORMAS, widget=forms.RadioSelect, initial=Lancamento.FORMA_PIX, required=False, label="Forma de pagamento"
        )
        self.fields["conta"] = forms.ModelChoiceField(
            ativos(Conta.objects.select_related("titular"), conta_id),
            required=False,
            label="Conta",
            widget=SelectContaComTipo,
        )
        self.fields["cartao"] = forms.ModelChoiceField(
            ativos(Cartao.objects.select_related("titular"), cartao_id), required=False, label="Cartão"
        )
        if conta_id or cartao_id:
            if forma_pagamento:
                forma = forma_pagamento
            elif cartao_id:
                forma = Lancamento.FORMA_CREDITO
            elif conta_tipo == "carteira":
                forma = Lancamento.FORMA_DINHEIRO
            else:
                forma = Lancamento.FORMA_PIX
            self.initial.update(forma=forma, conta=conta_id, cartao=cartao_id)

    def validar_pagamento(self, cd: dict, obrigatorio: bool = True) -> dict:
        forma = cd.get("forma") or Lancamento.FORMA_PIX
        if forma == Lancamento.FORMA_CREDITO:
            cd["conta"] = None
            if obrigatorio and not cd.get("cartao"):
                self.add_error("cartao", "Escolha o cartão de crédito.")
        elif forma == Lancamento.FORMA_DINHEIRO:
            cd["cartao"] = None
            conta = cd.get("conta")
            if obrigatorio and not conta:
                self.add_error("conta", "Escolha a carteira de dinheiro.")
            elif conta and conta.tipo != "carteira":
                self.add_error("conta", "Para pagamento em dinheiro, escolha uma conta do tipo Dinheiro / carteira.")
        else:
            if forma not in (Lancamento.FORMA_PIX, Lancamento.FORMA_DEBITO):
                forma = Lancamento.FORMA_PIX
            cd["cartao"] = None
            conta = cd.get("conta")
            if obrigatorio and not conta:
                self.add_error("conta", "Escolha a conta bancária.")
            elif conta and conta.tipo == "carteira":
                self.add_error("conta", "Para PIX ou Débito, escolha uma conta bancária (não uma carteira em dinheiro).")
        cd["forma"] = forma
        origem = cd.get("conta") or cd.get("cartao")
        if "centro" in self.fields and not cd.get("centro") and origem is not None and origem.centro_padrao_id:
            cd["centro"] = origem.centro_padrao
        return cd


class LancamentoForm(EstiloMixin, MeioPagamentoMixin, forms.ModelForm):
    TIPOS_FORM = [
        (Lancamento.RECEITA, "Receita"),
        (Lancamento.DESPESA, "Despesa"),
        (Lancamento.ESTORNO, "Estorno"),
        (Lancamento.TRANSFERENCIA, "Transferência"),
    ]

    forma = forms.ChoiceField(
        choices=FORMAS, widget=forms.RadioSelect, initial=Lancamento.FORMA_PIX, required=False, label="Forma de pagamento"
    )
    parcelas = forms.IntegerField(
        min_value=1,
        max_value=72,
        initial=1,
        required=False,
        label="Parcelas",
        help_text="Divide o valor em parcelas, uma por fatura.",
    )
    aplicar_futuras = forms.BooleanField(
        required=False,
        label="Aplicar descrição, categoria e centro às demais parcelas futuras",
    )

    field_order = [
        "tipo", "descricao", "valor", "data", "forma", "conta", "cartao", "parcelas", "conta_destino",
        "categoria", "centro", "veiculo", "pago", "vencimento", "compartilhado", "pago_por",
        "aplicar_futuras", "comprovante", "observacao",
    ]

    class Meta:
        model = Lancamento
        fields = [
            "tipo", "descricao", "valor", "data", "vencimento", "pago", "conta", "cartao",
            "conta_destino", "categoria", "centro", "veiculo", "compartilhado", "pago_por",
            "comprovante", "observacao",
        ]
        widgets = {
            "tipo": forms.RadioSelect,
            "conta": SelectContaComTipo,
            "categoria": SelectComTipo,
            "observacao": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        i = self.instance
        self.fields["tipo"].choices = self.TIPOS_FORM
        self.fields["conta"].queryset = ativos(Conta.objects.select_related("titular"), i.conta_id)
        self.fields["conta_destino"].queryset = ativos(Conta.objects.select_related("titular"), i.conta_destino_id)
        self.fields["cartao"].queryset = ativos(Cartao.objects.select_related("titular"), i.cartao_id)
        self.fields["categoria"].queryset = ativos(categorias_qs(), i.categoria_id)
        self.fields["centro"].queryset = ativos(CentroCusto.objects.all(), i.centro_id)
        self.fields["veiculo"].queryset = ativos(Veiculo.objects.all(), i.veiculo_id)
        self.fields["pago_por"].queryset = ativos(Titular.objects.filter(tipo=Titular.PF), i.pago_por_id)
        self.fields["pago_por"].empty_label = "Automático (titular do meio de pagamento)"
        if i.pk:
            del self.fields["parcelas"]
            self.initial["forma"] = _forma_inicial(i)
        if not (i.pk and i.grupo_parcelas):
            del self.fields["aplicar_futuras"]
        if not Veiculo.objects.exists():
            del self.fields["veiculo"]
        self.estilizar()

    def clean_comprovante(self):
        arq = self.cleaned_data.get("comprovante")
        if arq and hasattr(arq, "size"):
            validar_comprovante(arq)
        return arq

    def clean(self):
        cd = super().clean()
        tipo = cd.get("tipo")
        if tipo == Lancamento.TRANSFERENCIA:
            cd["cartao"] = None
            cd["categoria"] = None
            cd["compartilhado"] = False
            if not cd.get("conta"):
                self.add_error("conta", "Informe a conta de origem.")
            if not cd.get("conta_destino"):
                self.add_error("conta_destino", "Informe a conta de destino.")
            if cd.get("conta") and cd.get("conta_destino") and cd["conta"] == cd["conta_destino"]:
                self.add_error("conta_destino", "Origem e destino devem ser diferentes.")
        else:
            cd["conta_destino"] = None
            cd = self.validar_pagamento(cd, obrigatorio=True)
            if cd.get("cartao"):
                cd["pago"] = True
            cat = cd.get("categoria")
            if cat and tipo in (Lancamento.RECEITA, Lancamento.DESPESA, Lancamento.ESTORNO):
                esperado = Categoria.DESPESA if tipo == Lancamento.ESTORNO else tipo
                if cat.tipo != esperado:
                    self.add_error("categoria", "A categoria não corresponde ao tipo do lançamento.")

        categoria = cd.get("categoria")
        if categoria and "veiculo" in self.fields and not cd.get("veiculo"):
            raiz = categoria.pai or categoria
            if raiz.codigo_sistema == "veiculo" or raiz.nome.lower() in ("veículo", "veiculo"):
                veiculos = list(Veiculo.objects.filter(ativo=True)[:2])
                if len(veiculos) == 1:
                    cd["veiculo"] = veiculos[0]
        return cd

    def save(self, commit: bool = True):
        obj = super().save(commit=False)
        obj.forma_pagamento = (
            "" if obj.tipo == Lancamento.TRANSFERENCIA else (self.cleaned_data.get("forma") or "")
        )
        if commit:
            obj.save()
        return obj


class RecorrenciaForm(EstiloMixin, MeioPagamentoMixin, forms.ModelForm):
    forma = forms.ChoiceField(
        choices=FORMAS, widget=forms.RadioSelect, initial=Lancamento.FORMA_PIX, required=False, label="Forma de pagamento"
    )

    field_order = [
        "tipo", "descricao", "valor", "intervalo_meses", "dia", "forma", "conta", "cartao",
        "categoria", "centro", "veiculo", "inicio", "fim", "ativo",
    ]

    class Meta:
        model = Recorrencia
        fields = [
            "tipo", "descricao", "valor", "categoria", "centro", "conta", "cartao",
            "veiculo", "intervalo_meses", "dia", "inicio", "fim", "ativo",
        ]
        widgets = {"tipo": forms.RadioSelect, "conta": SelectContaComTipo, "categoria": SelectComTipo}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        i = self.instance
        self.fields["conta"].queryset = ativos(Conta.objects.select_related("titular"), i.conta_id)
        self.fields["cartao"].queryset = ativos(Cartao.objects.select_related("titular"), i.cartao_id)
        self.fields["categoria"].queryset = ativos(categorias_qs(), i.categoria_id)
        self.fields["centro"].queryset = ativos(CentroCusto.objects.all(), i.centro_id)
        self.fields["veiculo"].queryset = ativos(Veiculo.objects.all(), i.veiculo_id)
        if i.pk:
            self.initial["forma"] = _forma_inicial(i)
        if not Veiculo.objects.exists():
            del self.fields["veiculo"]
        self.estilizar()

    def clean(self):
        return self.validar_pagamento(super().clean(), obrigatorio=True)

    def save(self, commit: bool = True):
        obj = super().save(commit=False)
        obj.forma_pagamento = self.cleaned_data.get("forma") or ""
        if commit:
            obj.save()
        return obj


class AbastecimentoForm(EstiloMixin, MeioPagamentoMixin, forms.ModelForm):
    field_order = [
        "data", "km", "litros", "valor_total", "combustivel", "tanque_cheio", "posto", "forma", "conta", "cartao"
    ]

    class Meta:
        model = Abastecimento
        fields = ["data", "km", "litros", "valor_total", "combustivel", "tanque_cheio", "posto"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        lanc = self.instance.lancamento if self.instance.pk else None
        self.adicionar_pagamento(
            lanc.conta_id if lanc else None,
            lanc.cartao_id if lanc else None,
            forma_pagamento=lanc.forma_pagamento if lanc else "",
            conta_tipo=lanc.conta.tipo if (lanc and lanc.conta_id) else "",
        )
        self.fields["litros"].widget.attrs["step"] = "0.001"
        self.order_fields(self.field_order)
        self.estilizar()

    def clean(self):
        return self.validar_pagamento(super().clean())


class ManutencaoForm(EstiloMixin, MeioPagamentoMixin, forms.ModelForm):
    field_order = [
        "data", "servico", "km", "valor", "oficina", "forma", "conta", "cartao",
        "proxima_km", "proxima_data", "observacao",
    ]

    class Meta:
        model = Manutencao
        fields = ["data", "servico", "km", "valor", "oficina", "proxima_km", "proxima_data", "observacao"]
        widgets = {"observacao": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        lanc = self.instance.lancamento if self.instance.pk else None
        self.adicionar_pagamento(
            lanc.conta_id if lanc else None,
            lanc.cartao_id if lanc else None,
            forma_pagamento=lanc.forma_pagamento if lanc else "",
            conta_tipo=lanc.conta.tipo if (lanc and lanc.conta_id) else "",
        )
        self.fields["forma"].help_text = "Só é necessário quando há valor."
        self.order_fields(self.field_order)
        self.estilizar()

    def clean(self):
        cd = super().clean()
        return self.validar_pagamento(cd, obrigatorio=bool(cd.get("valor")))


class PagamentoFaturaForm(EstiloMixin, forms.Form):
    conta = forms.ModelChoiceField(Conta.objects.none(), label="Pagar com a conta")
    valor = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"), label="Valor pago")
    data = forms.DateField(label="Data do pagamento")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["conta"].queryset = Conta.objects.filter(ativo=True).select_related("titular")
        self.estilizar()


class OrcamentoForm(EstiloMixin, forms.ModelForm):
    vigencia_inicio = forms.DateField(
        required=False,
        initial=date(2000, 1, 1),
        label="Vigente a partir de",
        help_text="Opcional: deixe padrão (01/01/2000) para valer desde sempre.",
    )

    class Meta:
        model = Orcamento
        fields = ["categoria", "centro", "valor_mensal", "vigencia_inicio", "vigencia_fim", "ativo"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["categoria"].queryset = ativos(categorias_qs().filter(tipo="D"), self.instance.categoria_id)
        self.fields["centro"].queryset = ativos(CentroCusto.objects.all(), self.instance.centro_id)
        self.estilizar()

    def clean_vigencia_inicio(self):
        return self.cleaned_data.get("vigencia_inicio") or date(2000, 1, 1)


class ConfigForm(EstiloMixin, forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "pai" in self.fields:
            self.fields["pai"].queryset = (
                Categoria.objects.filter(pai__isnull=True).exclude(pk=self.instance.pk).order_by("tipo", "nome")
            )
            self.fields["pai"].label_from_instance = lambda c: f"{c.nome} ({c.get_tipo_display().lower()})"
        for nome in ("titular", "conta_pagamento"):
            if nome in self.fields:
                self.fields[nome].queryset = ativos(
                    self.fields[nome].queryset, getattr(self.instance, f"{nome}_id", None)
                )
        if "usuario" in self.fields:
            self.fields["usuario"].required = False
            self.fields["usuario"].queryset = User.objects.order_by("username")
        self.estilizar()


# ---------------------------------------------------------------------------
# Importação, Regras, Metas, Fechamento e Painel Admin
# ---------------------------------------------------------------------------


class ImportacaoForm(EstiloMixin, MeioPagamentoMixin, forms.Form):
    arquivo = forms.FileField(
        label="Arquivo de extrato (OFX ou CSV)",
        help_text="Aceita extratos bancários e faturas em OFX/QFX ou CSV (separado por ; ou ,).",
    )
    inverter_sinais_cartao = forms.BooleanField(
        required=False,
        label="Inverter sinais no cartão (CSV onde compras vêm positivas)",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.adicionar_pagamento()
        self.fields["forma"].choices = FORMAS_IMPORTACAO
        self.fields["forma"].initial = "conta"
        self.fields["forma"].label = "Destino"
        self.order_fields(["arquivo", "forma", "conta", "cartao", "inverter_sinais_cartao"])
        self.estilizar()

    def clean(self):
        cd = super(MeioPagamentoMixin, self).clean()
        forma = cd.get("forma") or "conta"
        if forma == "cartao":
            cd["conta"] = None
            if not cd.get("cartao"):
                self.add_error("cartao", "Escolha o cartão.")
        else:
            cd["cartao"] = None
            if not cd.get("conta"):
                self.add_error("conta", "Escolha a conta.")
        return cd


class RegraCategorizacaoForm(EstiloMixin, forms.ModelForm):
    class Meta:
        model = RegraCategorizacao
        fields = ["padrao", "tipo", "categoria", "centro", "prioridade", "ativo"]
        widgets = {"tipo": forms.RadioSelect, "categoria": SelectComTipo}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["categoria"].queryset = ativos(categorias_qs(), self.instance.categoria_id)
        self.fields["centro"].queryset = ativos(CentroCusto.objects.all(), self.instance.centro_id)
        self.estilizar()


class MetaFinanceiraForm(EstiloMixin, forms.ModelForm):
    class Meta:
        model = MetaFinanceira
        fields = ["nome", "valor_alvo", "valor_inicial", "prazo", "conta", "ativo"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["conta"].queryset = ativos(
            Conta.objects.select_related("titular"), self.instance.conta_id
        )
        self.estilizar()


class FechamentoForm(EstiloMixin, forms.ModelForm):
    class Meta:
        model = FechamentoMensal
        fields = ["observacao"]
        widgets = {"observacao": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.estilizar()


class UsuarioCriarForm(EstiloMixin, forms.Form):
    username = forms.CharField(max_length=150, label="Usuário (login)")
    first_name = forms.CharField(max_length=150, required=False, label="Nome")
    last_name = forms.CharField(max_length=150, required=False, label="Sobrenome")
    email = forms.EmailField(required=False, label="E-mail")
    password1 = forms.CharField(widget=forms.PasswordInput, label="Senha")
    password2 = forms.CharField(widget=forms.PasswordInput, label="Confirmar senha")
    titular = forms.ModelChoiceField(
        Titular.objects.filter(ativo=True), required=False, label="Vincular a um Titular"
    )
    telegram_chat_id = forms.CharField(max_length=64, required=False, label="Telegram Chat ID")
    is_staff = forms.BooleanField(required=False, label="Acesso ao Django Admin (/admin)")
    is_active = forms.BooleanField(required=False, initial=True, label="Usuário ativo")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.estilizar()

    def clean_username(self):
        u = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=u).exists():
            raise forms.ValidationError("Já existe um usuário com este login.")
        return u

    def clean(self):
        cd = super().clean()
        p1, p2 = cd.get("password1"), cd.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "As senhas não conferem.")
        elif p1:
            validate_password(p1)
        return cd


class UsuarioEditarForm(EstiloMixin, forms.ModelForm):
    titular = forms.ModelChoiceField(
        Titular.objects.filter(ativo=True), required=False, label="Titular vinculado"
    )
    telegram_chat_id = forms.CharField(max_length=64, required=False, label="Telegram Chat ID")

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "is_staff", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tit = getattr(self.instance, "titular", None)
        if tit:
            self.initial["titular"] = tit.pk
            self.initial["telegram_chat_id"] = tit.telegram_chat_id
        self.estilizar()


class UsuarioSenhaForm(EstiloMixin, forms.Form):
    nova_senha1 = forms.CharField(widget=forms.PasswordInput, label="Nova senha")
    nova_senha2 = forms.CharField(widget=forms.PasswordInput, label="Confirmar nova senha")

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario
        self.estilizar()

    def clean(self):
        cd = super().clean()
        p1, p2 = cd.get("nova_senha1"), cd.get("nova_senha2")
        if p1 and p2 and p1 != p2:
            self.add_error("nova_senha2", "As senhas não conferem.")
        elif p1:
            validate_password(p1, self.usuario)
        return cd


class IntegracaoTelegramForm(EstiloMixin, forms.Form):
    ativo = forms.BooleanField(required=False, label="Ativar integração com Telegram")
    token = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        label="Bot Token",
        help_text="Deixe em branco para manter o token já salvo.",
    )
    chat_id = forms.CharField(
        required=False,
        max_length=80,
        label="Chat ID padrão",
        help_text="ID do chat ou grupo onde os avisos serão enviados.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.estilizar()


class IntegracaoIAForm(EstiloMixin, forms.Form):
    PROVEDORES_IA = [
        ("gemini", "Google Gemini"),
        ("openai", "OpenAI"),
        ("anthropic", "Anthropic Claude"),
        ("ollama", "Ollama (local)"),
    ]
    ativo = forms.BooleanField(required=False, label="Habilitar integração com IA")
    provedor_ia = forms.ChoiceField(choices=PROVEDORES_IA, label="Provedor")
    modelo = forms.CharField(
        required=False, max_length=100, label="Modelo", help_text="Ex.: gemini-2.5-flash, gpt-4o-mini"
    )
    endpoint = forms.URLField(
        required=False, label="Endpoint customizado (opcional)", help_text="Útil para Ollama ou proxy local."
    )
    api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        label="API Key",
        help_text="Deixe em branco para manter a chave já salva.",
    )
    instrucoes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Contexto/regras para o assistente",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.estilizar()


class IntegracaoAlertasForm(EstiloMixin, forms.Form):
    ativo = forms.BooleanField(required=False, label="Ativar envio automático de alertas")
    dias_vencimento = forms.IntegerField(
        min_value=1, max_value=30, initial=3, label="Avisar contas vencendo em até (dias)"
    )
    orcamento_pct = forms.IntegerField(
        min_value=50, max_value=100, initial=80, label="Alertar orçamento ao atingir (%)"
    )
    incluir_veiculos = forms.BooleanField(required=False, initial=True, label="Incluir alertas de manutenção de veículos")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.estilizar()


class TokenAPIForm(EstiloMixin, forms.Form):
    nome = forms.CharField(max_length=80, label="Nome da integração", help_text="Ex.: Bot Telegram, Agente IA, n8n")
    usuario = forms.ModelChoiceField(User.objects.filter(is_active=True).order_by("username"), label="Usuário associado")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.estilizar()


class IntegracaoGitForm(EstiloMixin, forms.Form):
    repositorio = forms.CharField(
        max_length=200,
        label="Repositório no GitHub",
        help_text="Informe owner/repositorio ou URL do GitHub (ex.: usuario/financeiro-olpi).",
    )
    branch = forms.CharField(
        max_length=120,
        initial="main",
        label="Branch",
        help_text="Deve coincidir com a branch ativa no servidor (ex.: main).",
    )
    token = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        label="Token do GitHub (PAT)",
        help_text="Obrigatório apenas para repositórios privados (Contents: Read-only). Para repositório público, deixe em branco.",
    )
    remover_token = forms.BooleanField(
        required=False,
        label="Remover token salvo (usar acesso público sem autenticação)",
    )

    def __init__(self, *args, tem_token_salvo: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if not tem_token_salvo:
            self.fields.pop("remover_token", None)
        self.estilizar()

    def clean_repositorio(self):
        from .atualizacao import normalizar_repositorio

        norm = normalizar_repositorio(self.cleaned_data["repositorio"])
        if not norm:
            raise forms.ValidationError(
                "Informe um repositório válido no formato owner/repositorio ou URL do GitHub."
            )
        return norm

    def clean_branch(self):
        from .atualizacao import validar_branch

        br = validar_branch(self.cleaned_data["branch"])
        if not br:
            raise forms.ValidationError("Nome de branch inválido.")
        return br


class CicloFinanceiroForm(EstiloMixin, forms.ModelForm):
    class Meta:
        model = ConfiguracaoSistema
        fields = ["dia_inicio_ciclo", "referencia_ciclo"]
        widgets = {
            "referencia_ciclo": forms.RadioSelect,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.estilizar()
