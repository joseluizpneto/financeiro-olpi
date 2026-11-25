from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import (
    Case, Count, DecimalField as DBDecimalField, F, Max, OuterRef, Q, Subquery, Sum, When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.functional import cached_property

from .utils import dia_seguro, hoje, somar_meses

ZERO = Decimal("0.00")
DIA_DO_MES = [MinValueValidator(1), MaxValueValidator(31)]
DINHEIRO = dict(max_digits=12, decimal_places=2)
POSITIVO = [MinValueValidator(Decimal("0.01"))]


def _soma(qs: models.QuerySet, campo: str = "valor") -> Decimal:
    return qs.aggregate(t=Sum(campo))["t"] or ZERO


# ---------------------------------------------------------------------------
# Cadastros básicos
# ---------------------------------------------------------------------------


class Titular(models.Model):
    """Dono de contas e cartões: uma pessoa ou a empresa."""

    PF, PJ = "PF", "PJ"
    TIPOS = [(PF, "Pessoa física"), (PJ, "Pessoa jurídica")]

    nome = models.CharField("nome", max_length=100, unique=True)
    tipo = models.CharField("tipo", max_length=2, choices=TIPOS, default=PF)
    usuario = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="usuário vinculado",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="titular",
    )
    telegram_chat_id = models.CharField(
        "Telegram Chat ID",
        max_length=64,
        blank=True,
        help_text="Opcional: ID do chat para notificações individuais.",
    )
    ativo = models.BooleanField("ativo", default=True)

    class Meta:
        ordering = ["nome"]
        verbose_name = "titular"
        verbose_name_plural = "titulares"

    def __str__(self) -> str:
        return self.nome


class CentroCusto(models.Model):
    """Para onde vai o dinheiro: Casa, Veículo, Empresa..."""

    PESSOAL, EMPRESARIAL = "PF", "PJ"
    ESCOPOS = [(PESSOAL, "Pessoal"), (EMPRESARIAL, "Empresarial")]

    nome = models.CharField("nome", max_length=60, unique=True)
    descricao = models.CharField("descrição", max_length=200, blank=True)
    escopo = models.CharField(
        "escopo",
        max_length=2,
        choices=ESCOPOS,
        default=PESSOAL,
        help_text="Centros empresariais aparecem no relatório da empresa e no filtro Empresa (PJ).",
    )
    codigo_sistema = models.SlugField(
        "código interno", max_length=40, null=True, blank=True, unique=True, editable=False
    )
    ativo = models.BooleanField("ativo", default=True)

    class Meta:
        ordering = ["nome"]
        verbose_name = "centro de custo"
        verbose_name_plural = "centros de custo"

    def __str__(self) -> str:
        return self.nome


class Categoria(models.Model):
    RECEITA, DESPESA = "R", "D"
    TIPOS = [(RECEITA, "Receita"), (DESPESA, "Despesa")]

    nome = models.CharField("nome", max_length=80)
    tipo = models.CharField("tipo", max_length=1, choices=TIPOS, default=DESPESA)
    pai = models.ForeignKey(
        "self",
        verbose_name="categoria principal",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="filhas",
        limit_choices_to={"pai__isnull": True},
        help_text="Deixe vazio para criar uma categoria principal.",
    )
    imposto = models.BooleanField(
        "é imposto",
        default=False,
        help_text="Usado no relatório da empresa. Marcar na principal vale para as subcategorias.",
    )
    codigo_sistema = models.SlugField(
        "código interno", max_length=40, null=True, blank=True, unique=True, editable=False
    )
    ativo = models.BooleanField("ativo", default=True)

    class Meta:
        ordering = ["tipo", "nome"]
        verbose_name = "categoria"
        verbose_name_plural = "categorias"
        constraints = [
            models.UniqueConstraint(
                fields=["nome", "tipo", "pai"],
                condition=Q(pai__isnull=False),
                name="categoria_sub_unica",
            ),
            models.UniqueConstraint(
                fields=["nome", "tipo"],
                condition=Q(pai__isnull=True),
                name="categoria_raiz_unica",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.pai.nome} › {self.nome}" if self.pai_id else self.nome

    @property
    def eh_imposto(self) -> bool:
        return self.imposto or bool(self.pai_id and self.pai.imposto)

    def clean(self) -> None:
        if self.pai_id:
            if self.pai_id == self.pk:
                raise ValidationError({"pai": "A categoria não pode ser principal de si mesma."})
            if self.pai.pai_id:
                raise ValidationError({"pai": "Use no máximo dois níveis (principal › subcategoria)."})
            if self.pai.tipo != self.tipo:
                raise ValidationError({"pai": "A categoria principal deve ser do mesmo tipo."})


class Conta(models.Model):
    TIPOS = [
        ("corrente", "Conta corrente"),
        ("poupanca", "Poupança"),
        ("investimento", "Investimento"),
        ("carteira", "Dinheiro / carteira"),
        ("outro", "Outro"),
    ]

    banco = models.CharField("banco", max_length=80)
    tipo = models.CharField("tipo", max_length=20, choices=TIPOS, default="corrente")
    titular = models.ForeignKey(Titular, verbose_name="titular", on_delete=models.PROTECT, related_name="contas")
    saldo_inicial = models.DecimalField(
        "saldo inicial",
        **DINHEIRO,
        default=ZERO,
        help_text="Saldo da conta no dia em que você começou a usar o sistema.",
    )
    data_saldo_inicial = models.DateField("data do saldo inicial", default=timezone.localdate)
    centro_padrao = models.ForeignKey(
        CentroCusto,
        verbose_name="centro de custo padrão",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Preenchido automaticamente nos lançamentos desta conta (ex.: Empresa na conta PJ).",
    )
    conciliado_ate = models.DateField("conciliado até", null=True, blank=True)
    bloqueado_ate = models.DateField(
        "bloqueado para edições até",
        null=True,
        blank=True,
        help_text="Impede alterações em lançamentos com data até este dia.",
    )
    ativo = models.BooleanField("ativa", default=True)

    class Meta:
        ordering = ["titular__nome", "banco"]
        verbose_name = "conta bancária"
        verbose_name_plural = "contas bancárias"

    def __str__(self) -> str:
        return f"{self.banco} · {self.titular.nome}"


class Cartao(models.Model):
    banco = models.CharField("banco", max_length=80)
    titular = models.ForeignKey(Titular, verbose_name="titular", on_delete=models.PROTECT, related_name="cartoes")
    limite = models.DecimalField("limite", **DINHEIRO, null=True, blank=True, validators=POSITIVO)
    dia_fechamento = models.PositiveSmallIntegerField(
        "dia de fechamento",
        validators=DIA_DO_MES,
        help_text="Compras feitas a partir deste dia entram na fatura seguinte.",
    )
    dia_vencimento = models.PositiveSmallIntegerField("dia de vencimento", validators=DIA_DO_MES)
    conta_pagamento = models.ForeignKey(
        Conta,
        verbose_name="conta que paga a fatura",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    centro_padrao = models.ForeignKey(
        CentroCusto,
        verbose_name="centro de custo padrão",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    ativo = models.BooleanField("ativo", default=True)

    class Meta:
        ordering = ["banco"]
        verbose_name = "cartão de crédito"
        verbose_name_plural = "cartões de crédito"
        constraints = [
            models.CheckConstraint(
                condition=Q(dia_fechamento__gte=1, dia_fechamento__lte=31),
                name="cartao_dia_fechamento_valido",
            ),
            models.CheckConstraint(
                condition=Q(dia_vencimento__gte=1, dia_vencimento__lte=31),
                name="cartao_dia_vencimento_valido",
            ),
            models.CheckConstraint(
                condition=Q(limite__isnull=True) | Q(limite__gt=ZERO),
                name="cartao_limite_positivo",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.banco} · {self.titular.nome}"

    # --- regras de fatura -------------------------------------------------
    def _mes_fechamento_de(self, referencia: date) -> date:
        """Mês em que fecha a fatura cujo vencimento cai no mês `referencia`."""
        if self.dia_vencimento > self.dia_fechamento:
            return referencia
        return somar_meses(referencia, -1, dia=1)

    def referencia_para(self, data_compra: date) -> date:
        """Mês de vencimento (referência) da fatura em que entra uma compra."""
        fechamento = data_compra.replace(
            day=dia_seguro(data_compra.year, data_compra.month, self.dia_fechamento)
        )
        mes_fech = data_compra.replace(day=1)
        if data_compra >= fechamento:
            mes_fech = somar_meses(mes_fech, 1, dia=1)
        if self.dia_vencimento <= self.dia_fechamento:
            return somar_meses(mes_fech, 1, dia=1)
        return mes_fech

    def _datas_fatura(self, referencia: date) -> tuple[date, date, date]:
        referencia = referencia.replace(day=1)
        mes_fech = self._mes_fechamento_de(referencia)
        fechamento = mes_fech.replace(day=dia_seguro(mes_fech.year, mes_fech.month, self.dia_fechamento))
        vencimento = referencia.replace(day=dia_seguro(referencia.year, referencia.month, self.dia_vencimento))
        return referencia, fechamento, vencimento

    def fatura_virtual(self, referencia: date) -> Fatura:
        """Constrói objeto Fatura em memória sem persistir no banco (seguro para GET)."""
        referencia, fechamento, vencimento = self._datas_fatura(referencia)
        f = Fatura(cartao=self, referencia=referencia, fechamento=fechamento, vencimento=vencimento, paga=False)
        f.__dict__["total_calc"] = ZERO
        f.__dict__["pago_calc"] = ZERO
        f.__dict__["qtd_itens"] = 0
        return f

    def fatura_por_referencia(self, referencia: date) -> Fatura:
        referencia, fechamento, vencimento = self._datas_fatura(referencia)
        fatura = self.faturas.filter(referencia=referencia).first()
        if fatura:
            return fatura
        fatura, _ = Fatura.objects.get_or_create(
            cartao=self,
            referencia=referencia,
            defaults={"fechamento": fechamento, "vencimento": vencimento},
        )
        return fatura

    def fatura_para(self, data_compra: date) -> Fatura:
        return self.fatura_por_referencia(self.referencia_para(data_compra))

    def limite_utilizado(self) -> Decimal:
        faturas = Fatura.objects.com_totais().filter(cartao=self, paga=False)
        return max(sum((f.saldo for f in faturas), ZERO), ZERO)


class FaturaQuerySet(models.QuerySet):
    def com_totais(self) -> FaturaQuerySet:
        dec = DBDecimalField(max_digits=12, decimal_places=2)
        sub_itens = (
            Lancamento.objects.filter(fatura=OuterRef("pk"))
            .order_by()
            .values("fatura")
            .annotate(
                s=Sum(
                    Case(
                        When(tipo=Lancamento.DESPESA, then=F("valor")),
                        When(tipo__in=[Lancamento.RECEITA, Lancamento.ESTORNO], then=-F("valor")),
                        default=ZERO,
                        output_field=dec,
                    )
                )
            )
            .values("s")[:1]
        )
        sub_qtd = (
            Lancamento.objects.filter(fatura=OuterRef("pk"))
            .order_by()
            .values("fatura")
            .annotate(c=Count("id"))
            .values("c")[:1]
        )
        sub_pg = (
            Lancamento.objects.filter(fatura_paga=OuterRef("pk"), pago=True)
            .order_by()
            .values("fatura_paga")
            .annotate(s=Sum("valor"))
            .values("s")[:1]
        )
        return self.annotate(
            total_calc=Coalesce(Subquery(sub_itens, output_field=dec), ZERO),
            pago_calc=Coalesce(Subquery(sub_pg, output_field=dec), ZERO),
            qtd_itens=Coalesce(Subquery(sub_qtd, output_field=models.IntegerField()), 0),
        )


class Fatura(models.Model):
    cartao = models.ForeignKey(Cartao, verbose_name="cartão", on_delete=models.PROTECT, related_name="faturas")
    referencia = models.DateField("mês de referência", help_text="1º dia do mês de vencimento.")
    fechamento = models.DateField("fechamento")
    vencimento = models.DateField("vencimento")
    paga = models.BooleanField("paga", default=False)

    objects = FaturaQuerySet.as_manager()

    class Meta:
        ordering = ["-referencia"]
        verbose_name = "fatura"
        verbose_name_plural = "faturas"
        constraints = [
            models.UniqueConstraint(fields=["cartao", "referencia"], name="fatura_unica_por_mes"),
        ]
        indexes = [
            models.Index(fields=["paga", "vencimento"]),
            models.Index(fields=["cartao", "paga"]),
        ]

    def __str__(self) -> str:
        return f"{self.cartao.banco} · {self.referencia:%m/%Y}"

    @cached_property
    def total(self) -> Decimal:
        if "total_calc" in self.__dict__:
            return self.__dict__["total_calc"]
        if not self.pk:
            return ZERO
        dec = DBDecimalField(max_digits=12, decimal_places=2)
        return (
            self.lancamentos.aggregate(
                t=Coalesce(
                    Sum(
                        Case(
                            When(tipo=Lancamento.DESPESA, then=F("valor")),
                            When(tipo__in=[Lancamento.RECEITA, Lancamento.ESTORNO], then=-F("valor")),
                            default=ZERO,
                            output_field=dec,
                        )
                    ),
                    ZERO,
                )
            )["t"]
            or ZERO
        )

    @cached_property
    def valor_pago(self) -> Decimal:
        if "pago_calc" in self.__dict__:
            return self.__dict__["pago_calc"]
        if not self.pk:
            return ZERO
        return _soma(self.pagamentos.filter(pago=True))

    @property
    def saldo(self) -> Decimal:
        return self.total - self.valor_pago

    @property
    def situacao(self) -> str:
        if self.paga:
            return "Paga"
        h = hoje()
        if h < self.fechamento:
            return "Aberta"
        if h > self.vencimento:
            return "Vencida"
        return "Fechada"

    def atualizar_status(self) -> None:
        for campo in ("total", "valor_pago", "total_calc", "pago_calc", "qtd_itens"):
            self.__dict__.pop(campo, None)
        if not self.pk:
            return
        qtd = self.lancamentos.count()
        total = self.total
        pago = self.valor_pago
        paga = (qtd > 0 and total <= ZERO) or (total > ZERO and pago >= total)
        if paga != self.paga:
            self.paga = paga
            self.save(update_fields=["paga"])


# ---------------------------------------------------------------------------
# Veículo
# ---------------------------------------------------------------------------


class Veiculo(models.Model):
    nome = models.CharField("nome", max_length=80, help_text="Ex.: Carro principal")
    marca = models.CharField("marca", max_length=60, blank=True)
    modelo = models.CharField("modelo", max_length=60, blank=True)
    ano = models.PositiveSmallIntegerField("ano", null=True, blank=True)
    placa = models.CharField("placa", max_length=10, blank=True)
    km_inicial = models.PositiveIntegerField(
        "km inicial", default=0, help_text="Hodômetro quando começou a usar o sistema."
    )
    centro = models.ForeignKey(
        CentroCusto,
        verbose_name="centro de custo",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Usado nos lançamentos gerados por abastecimentos e manutenções.",
    )
    ativo = models.BooleanField("ativo", default=True)

    class Meta:
        ordering = ["nome"]
        verbose_name = "veículo"
        verbose_name_plural = "veículos"

    def __str__(self) -> str:
        return self.nome

    def km_atual(self) -> int:
        a = self.abastecimentos.aggregate(m=Max("km"))["m"] or 0
        m = self.manutencoes.aggregate(m=Max("km"))["m"] or 0
        return max(self.km_inicial, a, m)


# ---------------------------------------------------------------------------
# Recorrências e lançamentos
# ---------------------------------------------------------------------------


class Recorrencia(models.Model):
    FREQUENCIAS = [(1, "Mensal"), (2, "Bimestral"), (3, "Trimestral"), (6, "Semestral"), (12, "Anual")]

    descricao = models.CharField("descrição", max_length=200)
    tipo = models.CharField("tipo", max_length=1, choices=Categoria.TIPOS, default=Categoria.DESPESA)
    valor = models.DecimalField("valor", **DINHEIRO, validators=POSITIVO)
    categoria = models.ForeignKey(
        Categoria, verbose_name="categoria", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    centro = models.ForeignKey(
        CentroCusto, verbose_name="centro de custo", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    conta = models.ForeignKey(
        Conta, verbose_name="conta", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cartao = models.ForeignKey(
        Cartao, verbose_name="cartão", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    veiculo = models.ForeignKey(
        Veiculo, verbose_name="veículo", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    intervalo_meses = models.PositiveSmallIntegerField("frequência", choices=FREQUENCIAS, default=1)
    dia = models.PositiveSmallIntegerField("dia do vencimento", validators=DIA_DO_MES)
    inicio = models.DateField("início", default=timezone.localdate)
    fim = models.DateField("fim", null=True, blank=True, help_text="Deixe vazio se não tem data para acabar.")
    ultima_gerada = models.DateField("última gerada", null=True, blank=True, editable=False)
    forma_pagamento = models.CharField(
        "forma de pagamento",
        max_length=12,
        choices=[("pix", "PIX"), ("debito", "Débito"), ("credito", "Crédito"), ("dinheiro", "Dinheiro")],
        blank=True,
        default="",
    )
    ativo = models.BooleanField("ativa", default=True)
    criado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False
    )
    atualizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False
    )
    criado_em = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ["dia", "descricao"]
        verbose_name = "recorrência"
        verbose_name_plural = "recorrências"
        constraints = [
            models.CheckConstraint(condition=Q(valor__gt=ZERO), name="recorrencia_valor_positivo"),
            models.CheckConstraint(condition=Q(dia__gte=1, dia__lte=31), name="recorrencia_dia_valido"),
            models.CheckConstraint(
                condition=(Q(conta__isnull=False) & Q(cartao__isnull=True))
                | (Q(conta__isnull=True) & Q(cartao__isnull=False)),
                name="recorrencia_meio_unico",
            ),
            models.CheckConstraint(
                condition=Q(fim__isnull=True) | Q(fim__gte=F("inicio")),
                name="recorrencia_fim_valido",
            ),
        ]

    def __str__(self) -> str:
        return self.descricao

    def clean(self) -> None:
        if bool(self.conta_id) == bool(self.cartao_id):
            raise ValidationError("Informe uma conta ou um cartão.")
        if self.categoria_id and self.categoria.tipo != self.tipo:
            raise ValidationError({"categoria": "A categoria não corresponde ao tipo."})
        if self.fim and self.fim < self.inicio:
            raise ValidationError({"fim": "O fim deve ser depois do início."})

    def datas(self, ate: date):
        """Todas as ocorrências entre o início e `ate` (respeitando o fim)."""
        base = self.inicio.replace(day=1)
        k = 0
        while True:
            d = somar_meses(base, k * self.intervalo_meses, dia=self.dia)
            k += 1
            if d < self.inicio:
                continue
            if d > ate or (self.fim and d > self.fim):
                return
            yield d

    def proxima(self, a_partir: date | None = None) -> date | None:
        a_partir = a_partir or hoje()
        for d in self.datas(a_partir + timedelta(days=400)):
            if d >= a_partir:
                return d
        return None

    @property
    def valor_mensal(self) -> Decimal:
        return (self.valor / self.intervalo_meses).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def gerar(self, ate: date) -> int:
        """Cria os lançamentos pendentes até `ate`. Retorna quantos criou."""
        existentes_meses = set(self.lancamentos.values_list("data__year", "data__month"))
        existentes_datas = set(self.lancamentos.values_list("data", flat=True))
        criados, ultima = 0, None
        for d in self.datas(ate):
            if self.ultima_gerada and d <= self.ultima_gerada:
                continue
            if d in existentes_datas or (d.year, d.month) in existentes_meses:
                ultima = d
                continue
            Lancamento(
                tipo=self.tipo,
                descricao=self.descricao,
                valor=self.valor,
                data=d,
                vencimento=d,
                categoria=self.categoria,
                centro=self.centro,
                conta=self.conta,
                cartao=self.cartao,
                forma_pagamento=self.forma_pagamento,
                veiculo=self.veiculo,
                recorrencia=self,
                pago=bool(self.cartao_id),
                criado_por=self.criado_por,
                atualizado_por=self.atualizado_por,
            ).save()
            existentes_datas.add(d)
            existentes_meses.add((d.year, d.month))
            criados += 1
            ultima = d
        if ultima and (self.ultima_gerada is None or ultima > self.ultima_gerada):
            self.ultima_gerada = ultima
            self.save(update_fields=["ultima_gerada"])
        return criados


class Lancamento(models.Model):
    RECEITA, DESPESA, TRANSFERENCIA, PAGAMENTO_FATURA, ESTORNO = "R", "D", "T", "F", "E"
    TIPOS = [
        (RECEITA, "Receita"),
        (DESPESA, "Despesa"),
        (TRANSFERENCIA, "Transferência"),
        (PAGAMENTO_FATURA, "Pagamento de fatura"),
        (ESTORNO, "Estorno / reembolso"),
    ]
    FORMA_PIX, FORMA_DEBITO, FORMA_CREDITO, FORMA_DINHEIRO = "pix", "debito", "credito", "dinheiro"
    FORMAS_PAGAMENTO = [
        (FORMA_PIX, "PIX"),
        (FORMA_DEBITO, "Débito"),
        (FORMA_CREDITO, "Crédito"),
        (FORMA_DINHEIRO, "Dinheiro"),
    ]
    NAO_FINANCEIROS_UPDATE = frozenset(
        {"conciliado", "conciliado_em", "comprovante", "observacao", "atualizado_por", "atualizado_em"}
    )

    tipo = models.CharField("tipo", max_length=1, choices=TIPOS, default=DESPESA)
    forma_pagamento = models.CharField(
        "forma de pagamento", max_length=12, choices=FORMAS_PAGAMENTO, blank=True, default=""
    )
    descricao = models.CharField("descrição", max_length=200)
    valor = models.DecimalField("valor", **DINHEIRO, validators=POSITIVO)
    data = models.DateField(
        "data",
        default=timezone.localdate,
        help_text="Data da compra ou competência. No cartão, define em qual fatura entra.",
    )
    vencimento = models.DateField(
        "vencimento",
        null=True,
        blank=True,
        help_text="Para contas a pagar ou a receber. Enquanto não for paga, aparece nesta data na lista de lançamentos.",
    )
    pago = models.BooleanField("já foi pago", default=True)
    data_pagamento = models.DateField("data do pagamento", null=True, blank=True)

    conta = models.ForeignKey(
        Conta, verbose_name="conta", null=True, blank=True, on_delete=models.PROTECT, related_name="lancamentos"
    )
    conta_destino = models.ForeignKey(
        Conta,
        verbose_name="conta de destino",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="transferencias_recebidas",
    )
    cartao = models.ForeignKey(
        Cartao, verbose_name="cartão", null=True, blank=True, on_delete=models.PROTECT, related_name="lancamentos"
    )
    fatura = models.ForeignKey(
        Fatura,
        verbose_name="fatura",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="lancamentos",
        editable=False,
    )
    fatura_paga = models.ForeignKey(
        Fatura,
        verbose_name="fatura paga",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="pagamentos",
    )

    categoria = models.ForeignKey(
        Categoria, verbose_name="categoria", null=True, blank=True, on_delete=models.PROTECT, related_name="lancamentos"
    )
    centro = models.ForeignKey(
        CentroCusto,
        verbose_name="centro de custo",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="lancamentos",
    )
    veiculo = models.ForeignKey(
        Veiculo, verbose_name="veículo", null=True, blank=True, on_delete=models.SET_NULL, related_name="lancamentos"
    )
    recorrencia = models.ForeignKey(
        Recorrencia, null=True, blank=True, on_delete=models.SET_NULL, related_name="lancamentos", editable=False
    )
    estorno_de = models.ForeignKey(
        "self",
        verbose_name="estorno referente a",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="estornos",
    )

    grupo_parcelas = models.UUIDField(null=True, blank=True, editable=False, db_index=True)
    parcela = models.PositiveSmallIntegerField(null=True, blank=True, editable=False)
    total_parcelas = models.PositiveSmallIntegerField(null=True, blank=True, editable=False)

    compartilhado = models.BooleanField(
        "despesa compartilhada (rateio)",
        default=False,
        help_text="Marca para entrar no cálculo de rateio mensal entre os titulares.",
    )
    pago_por = models.ForeignKey(
        Titular,
        verbose_name="pago por",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    conciliado = models.BooleanField("conciliado", default=False)
    conciliado_em = models.DateTimeField("conciliado em", null=True, blank=True, editable=False)
    comprovante = models.FileField("comprovante / boleto", upload_to="comprovantes/%Y/%m/", null=True, blank=True)
    hash_importacao = models.CharField(
        "hash de importação", max_length=64, null=True, blank=True, unique=True, editable=False
    )

    observacao = models.TextField("observação", blank=True)
    criado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False
    )
    atualizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False
    )
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-data", "-id"]
        verbose_name = "lançamento"
        verbose_name_plural = "lançamentos"
        indexes = [
            models.Index(fields=["data", "id"]),
            models.Index(fields=["tipo", "data"]),
            models.Index(fields=["pago", "vencimento"]),
            models.Index(fields=["pago", "data_pagamento"]),
            models.Index(fields=["conta", "pago", "data_pagamento"]),
            models.Index(fields=["conta_destino", "pago", "data_pagamento"]),
            models.Index(fields=["centro", "tipo", "data"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(valor__gt=ZERO), name="lancamento_valor_positivo"),
            models.CheckConstraint(
                condition=~Q(tipo="T")
                | (Q(conta__isnull=False) & Q(conta_destino__isnull=False) & ~Q(conta=F("conta_destino"))),
                name="lancamento_transferencia_valida",
            ),
            models.CheckConstraint(
                condition=~Q(tipo__in=["R", "D", "E"])
                | (
                    (Q(conta__isnull=False) & Q(cartao__isnull=True))
                    | (Q(conta__isnull=True) & Q(cartao__isnull=False))
                ),
                name="lancamento_meio_unico",
            ),
            models.CheckConstraint(
                condition=~Q(tipo="F") | (Q(conta__isnull=False) & Q(fatura_paga__isnull=False)),
                name="lancamento_pagamento_fatura_valido",
            ),
            models.UniqueConstraint(
                fields=["recorrencia", "data"],
                condition=Q(recorrencia__isnull=False),
                name="lancamento_recorrencia_data_unica",
            ),
        ]

    def __str__(self) -> str:
        return self.descricao

    def clean(self) -> None:
        if self.tipo in (self.RECEITA, self.DESPESA, self.ESTORNO):
            if bool(self.conta_id) == bool(self.cartao_id):
                raise ValidationError("Informe uma conta ou um cartão (apenas um).")
            if self.categoria_id:
                tipo_esperado = Categoria.DESPESA if self.tipo == self.ESTORNO else self.tipo
                if self.categoria.tipo != tipo_esperado:
                    raise ValidationError({"categoria": "A categoria não corresponde ao tipo do lançamento."})
        elif self.tipo == self.TRANSFERENCIA:
            if not self.conta_id or not self.conta_destino_id:
                raise ValidationError("Informe a conta de origem e a de destino.")
            if self.conta_id == self.conta_destino_id:
                raise ValidationError({"conta_destino": "Origem e destino devem ser diferentes."})
            if self.cartao_id:
                raise ValidationError("Transferência não pode usar cartão.")
        elif self.tipo == self.PAGAMENTO_FATURA:
            if not self.conta_id or not self.fatura_paga_id:
                raise ValidationError("Pagamento de fatura precisa de conta e fatura.")
            if self.cartao_id:
                raise ValidationError("Pagamento de fatura deve sair de uma conta.")

    @transaction.atomic
    def save(self, *args, manter_fatura: bool = False, **kwargs) -> None:
        update_fields = kwargs.get("update_fields")
        if update_fields and set(update_fields).issubset(self.NAO_FINANCEIROS_UPDATE):
            super().save(*args, **kwargs)
            return
        if update_fields:
            kwargs.pop("update_fields", None)

        fatura_antiga = None
        fatura_paga_antiga = None
        if self.pk:
            antigo = Lancamento.objects.filter(pk=self.pk).values("fatura_id", "fatura_paga_id").first()
            if antigo:
                fatura_antiga = antigo["fatura_id"]
                fatura_paga_antiga = antigo["fatura_paga_id"]

        if self.tipo in (self.RECEITA, self.DESPESA, self.ESTORNO) and self.cartao_id:
            self.conta = None
            if not (manter_fatura and self.fatura_id):
                self.fatura = self.cartao.fatura_para(self.data)
            self.pago = True
            self.forma_pagamento = self.FORMA_CREDITO
        else:
            self.cartao = None
            self.fatura = None
            if self.tipo in (self.RECEITA, self.DESPESA, self.ESTORNO) and self.conta_id:
                if self.conta.tipo == "carteira":
                    self.forma_pagamento = self.FORMA_DINHEIRO
                elif self.forma_pagamento not in (self.FORMA_PIX, self.FORMA_DEBITO):
                    self.forma_pagamento = self.FORMA_PIX
            else:
                self.forma_pagamento = ""

        if self.tipo != self.TRANSFERENCIA:
            self.conta_destino = None
        if self.tipo != self.PAGAMENTO_FATURA:
            self.fatura_paga = None

        if not self.centro_id and self.tipo in (self.RECEITA, self.DESPESA, self.ESTORNO):
            origem = self.cartao if self.cartao_id else self.conta
            if origem is not None and origem.centro_padrao_id:
                self.centro_id = origem.centro_padrao_id

        if not self.pago_por_id and self.tipo in (self.DESPESA, self.ESTORNO):
            if self.cartao_id and self.cartao.titular_id:
                self.pago_por_id = self.cartao.titular_id
            elif self.conta_id and self.conta.titular_id:
                self.pago_por_id = self.conta.titular_id

        if not self.vencimento:
            self.vencimento = self.data
        if self.pago:
            self.data_pagamento = self.data_pagamento or self.data
        else:
            self.data_pagamento = None

        super().save(*args, **kwargs)
        self._atualizar_faturas({fatura_antiga, fatura_paga_antiga, self.fatura_id, self.fatura_paga_id})

    @transaction.atomic
    def delete(self, *args, **kwargs):
        ids = {self.fatura_id, self.fatura_paga_id}
        resultado = super().delete(*args, **kwargs)
        self._atualizar_faturas(ids)
        return resultado

    @staticmethod
    def _atualizar_faturas(ids: set[int | None]) -> None:
        limpos = {i for i in ids if i}
        for f in Fatura.objects.filter(pk__in=limpos):
            f.atualizar_status()

    @property
    def data_exibicao(self) -> date:
        """Data exibida na listagem: vencimento para pendentes, data da compra/competência para pagos."""
        if not self.pago and self.vencimento:
            return self.vencimento
        return self.data

    @property
    def situacao(self) -> str:
        if self.pago:
            return "pago"
        if self.vencimento and self.vencimento < hoje():
            return "atrasado"
        return "pendente"

    @property
    def parcela_label(self) -> str:
        return f"{self.parcela}/{self.total_parcelas}" if self.parcela else ""

    @property
    def meio(self):
        if self.cartao_id:
            return self.cartao
        return self.conta


class Abastecimento(models.Model):
    COMBUSTIVEIS = [("gasolina", "Gasolina"), ("etanol", "Etanol"), ("diesel", "Diesel"), ("gnv", "GNV")]

    veiculo = models.ForeignKey(Veiculo, on_delete=models.CASCADE, related_name="abastecimentos")
    data = models.DateField("data", default=timezone.localdate)
    km = models.PositiveIntegerField("hodômetro (km)")
    litros = models.DecimalField("litros", max_digits=8, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))])
    valor_total = models.DecimalField("valor total", **DINHEIRO, validators=POSITIVO)
    combustivel = models.CharField("combustível", max_length=10, choices=COMBUSTIVEIS, default="gasolina")
    tanque_cheio = models.BooleanField(
        "encheu o tanque", default=True, help_text="Necessário para calcular o consumo (km/l)."
    )
    posto = models.CharField("posto", max_length=100, blank=True)
    lancamento = models.OneToOneField(
        Lancamento, null=True, blank=True, on_delete=models.SET_NULL, related_name="abastecimento", editable=False
    )
    criado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False
    )
    atualizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False
    )
    criado_em = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ["-data", "-km"]
        verbose_name = "abastecimento"
        verbose_name_plural = "abastecimentos"
        constraints = [
            models.CheckConstraint(condition=Q(litros__gt=Decimal("0.000")), name="abastecimento_litros_positivo"),
            models.CheckConstraint(condition=Q(valor_total__gt=ZERO), name="abastecimento_valor_positivo"),
        ]

    def __str__(self) -> str:
        return f"{self.data:%d/%m/%Y} · {self.litros} L"

    @property
    def preco_litro(self) -> Decimal | None:
        return (self.valor_total / self.litros).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP) if self.litros else None


class Manutencao(models.Model):
    veiculo = models.ForeignKey(Veiculo, on_delete=models.CASCADE, related_name="manutencoes")
    data = models.DateField("data", default=timezone.localdate)
    km = models.PositiveIntegerField("hodômetro (km)", null=True, blank=True)
    servico = models.CharField("serviço", max_length=150, help_text="Ex.: Troca de óleo, Revisão 30 mil, Pneus")
    valor = models.DecimalField("valor", **DINHEIRO, default=ZERO, validators=[MinValueValidator(ZERO)])
    oficina = models.CharField("oficina", max_length=100, blank=True)
    proxima_km = models.PositiveIntegerField(
        "próxima em (km)", null=True, blank=True, help_text="Hodômetro da próxima troca/revisão."
    )
    proxima_data = models.DateField("próxima em (data)", null=True, blank=True)
    observacao = models.TextField("observação", blank=True)
    lancamento = models.OneToOneField(
        Lancamento, null=True, blank=True, on_delete=models.SET_NULL, related_name="manutencao", editable=False
    )
    criado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False
    )
    atualizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False
    )
    criado_em = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ["-data", "-id"]
        verbose_name = "manutenção"
        verbose_name_plural = "manutenções"
        constraints = [
            models.CheckConstraint(condition=Q(valor__gte=ZERO), name="manutencao_valor_nao_negativo"),
        ]

    def __str__(self) -> str:
        return self.servico


class Orcamento(models.Model):
    categoria = models.ForeignKey(
        Categoria,
        verbose_name="categoria",
        on_delete=models.PROTECT,
        related_name="orcamentos",
        limit_choices_to={"tipo": Categoria.DESPESA},
        help_text="Numa categoria principal, conta também as subcategorias.",
    )
    centro = models.ForeignKey(
        CentroCusto,
        verbose_name="centro de custo",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Opcional: limitar o orçamento a um centro de custo.",
    )
    valor_mensal = models.DecimalField("limite mensal", **DINHEIRO, validators=POSITIVO)
    vigencia_inicio = models.DateField("vigente a partir de", default=date(2000, 1, 1))
    vigencia_fim = models.DateField("vigente até", null=True, blank=True)
    ativo = models.BooleanField("ativo", default=True)

    class Meta:
        ordering = ["categoria__nome"]
        verbose_name = "orçamento"
        verbose_name_plural = "orçamentos"
        constraints = [
            models.UniqueConstraint(
                fields=["categoria", "vigencia_inicio"],
                condition=Q(centro__isnull=True),
                name="orcamento_sem_centro_unico",
            ),
            models.UniqueConstraint(
                fields=["categoria", "centro", "vigencia_inicio"],
                condition=Q(centro__isnull=False),
                name="orcamento_com_centro_unico",
            ),
            models.CheckConstraint(condition=Q(valor_mensal__gt=ZERO), name="orcamento_valor_positivo"),
            models.CheckConstraint(
                condition=Q(vigencia_fim__isnull=True) | Q(vigencia_fim__gte=F("vigencia_inicio")),
                name="orcamento_vigencia_valida",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.categoria} · {self.valor_mensal}"

    def clean(self) -> None:
        if self.vigencia_fim and self.vigencia_fim < self.vigencia_inicio:
            raise ValidationError({"vigencia_fim": "A vigência final deve ser maior ou igual à inicial."})


# ---------------------------------------------------------------------------
# Auditoria, Integrações, Segurança e Planejamento
# ---------------------------------------------------------------------------


class RegistroAuditoria(models.Model):
    CRIAR, EDITAR, EXCLUIR, PAGAR, CONCILIAR, IMPORTAR, ADMIN, LOGIN = (
        "criar",
        "editar",
        "excluir",
        "pagar",
        "conciliar",
        "importar",
        "admin",
        "login",
    )
    ACOES = [
        (CRIAR, "Criação"),
        (EDITAR, "Edição"),
        (EXCLUIR, "Exclusão"),
        (PAGAR, "Pagamento/baixa"),
        (CONCILIAR, "Conciliação"),
        (IMPORTAR, "Importação"),
        (ADMIN, "Administração"),
        (LOGIN, "Acesso/autenticação"),
    ]

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="usuário",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="registros_auditoria",
    )
    acao = models.CharField("ação", max_length=20, choices=ACOES)
    modelo = models.CharField("entidade", max_length=60)
    objeto_id = models.CharField("ID", max_length=64, blank=True)
    resumo = models.CharField("resumo", max_length=255)
    detalhes = models.JSONField("detalhes", default=dict, blank=True)
    ip = models.GenericIPAddressField("IP", null=True, blank=True)
    criado_em = models.DateTimeField("data/hora", auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-criado_em", "-id"]
        verbose_name = "registro de auditoria"
        verbose_name_plural = "registros de auditoria"
        indexes = [
            models.Index(fields=["modelo", "objeto_id"]),
            models.Index(fields=["acao", "criado_em"]),
        ]

    def __str__(self) -> str:
        return f"[{self.acao}] {self.resumo}"


class Integracao(models.Model):
    TELEGRAM, IA, ALERTAS, EMAIL, GIT = "telegram", "ia", "alertas", "email", "git"
    PROVEDORES = [
        (TELEGRAM, "Bot do Telegram"),
        (IA, "Assistente de IA (LLM)"),
        (ALERTAS, "Alertas automáticos"),
        (EMAIL, "Notificações por e-mail"),
        (GIT, "Atualização via Git"),
    ]

    provedor = models.CharField("provedor", max_length=20, choices=PROVEDORES, unique=True)
    ativo = models.BooleanField("ativo", default=False)
    config = models.JSONField("configuração", default=dict, blank=True)
    segredo = models.TextField("chave/token secreto", blank=True)
    atualizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provedor"]
        verbose_name = "integração"
        verbose_name_plural = "integrações"

    def __str__(self) -> str:
        return self.get_provedor_display()

    def segredo_mascarado(self) -> str:
        if not self.segredo:
            return ""
        if len(self.segredo) <= 8:
            return "••••"
        return f"{self.segredo[:4]}••••{self.segredo[-4:]}"


class TokenAPI(models.Model):
    nome = models.CharField("identificação", max_length=80, help_text="Ex.: Bot Telegram, Agente IA, n8n")
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="usuário associado", on_delete=models.CASCADE, related_name="tokens_api"
    )
    prefixo = models.CharField("prefixo", max_length=16)
    token_hash = models.CharField("hash SHA-256", max_length=64, unique=True)
    ativo = models.BooleanField("ativo", default=True)
    ultimo_uso = models.DateTimeField("último uso", null=True, blank=True)
    criado_em = models.DateTimeField("criado em", auto_now_add=True)

    class Meta:
        ordering = ["-criado_em"]
        verbose_name = "token de API"
        verbose_name_plural = "tokens de API"

    def __str__(self) -> str:
        return f"{self.nome} ({self.prefixo}…)"


class TentativaLogin(models.Model):
    chave = models.CharField(max_length=180, unique=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    username = models.CharField(max_length=150, blank=True)
    falhas = models.PositiveSmallIntegerField(default=0)
    bloqueado_ate = models.DateTimeField(null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "tentativa de login"
        verbose_name_plural = "tentativas de login"

    def __str__(self) -> str:
        return f"{self.chave} ({self.falhas})"


class RegraCategorizacao(models.Model):
    padrao = models.CharField(
        "texto na descrição",
        max_length=120,
        help_text="Busca sem diferenciar maiúsculas/minúsculas (ex.: IFOOD, POSTO, UBER).",
    )
    tipo = models.CharField("tipo", max_length=1, choices=Categoria.TIPOS, default=Categoria.DESPESA)
    categoria = models.ForeignKey(
        Categoria, verbose_name="categoria sugerida", on_delete=models.CASCADE, related_name="regras"
    )
    centro = models.ForeignKey(
        CentroCusto, verbose_name="centro de custo", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    prioridade = models.PositiveSmallIntegerField(
        "prioridade", default=10, help_text="Menor número é avaliado primeiro."
    )
    ativo = models.BooleanField("ativa", default=True)

    class Meta:
        ordering = ["prioridade", "padrao"]
        verbose_name = "regra de categorização"
        verbose_name_plural = "regras de categorização"

    def __str__(self) -> str:
        return f"{self.padrao} → {self.categoria}"

    def clean(self) -> None:
        if self.categoria_id and self.categoria.tipo != self.tipo:
            raise ValidationError({"categoria": "A categoria deve ser do mesmo tipo da regra."})


class MetaFinanceira(models.Model):
    nome = models.CharField("nome da meta", max_length=120)
    valor_alvo = models.DecimalField("valor objetivo", **DINHEIRO, validators=POSITIVO)
    valor_inicial = models.DecimalField(
        "valor já reservado", **DINHEIRO, default=ZERO, validators=[MinValueValidator(ZERO)]
    )
    prazo = models.DateField("prazo desejado", null=True, blank=True)
    conta = models.ForeignKey(
        Conta,
        verbose_name="conta vinculada (opcional)",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="metas",
        help_text="Se informada, soma o saldo atual dessa conta ao valor reservado.",
    )
    ativo = models.BooleanField("ativa", default=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["prazo", "nome"]
        verbose_name = "meta financeira"
        verbose_name_plural = "metas financeiras"
        constraints = [
            models.CheckConstraint(condition=Q(valor_alvo__gt=ZERO), name="meta_valor_alvo_positivo"),
            models.CheckConstraint(condition=Q(valor_inicial__gte=ZERO), name="meta_valor_inicial_nao_negativo"),
        ]

    def __str__(self) -> str:
        return self.nome


class FechamentoMensal(models.Model):
    mes = models.DateField("mês de referência", unique=True, help_text="1º dia do mês.")
    fechado = models.BooleanField("mês fechado", default=False)
    fechado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    fechado_em = models.DateTimeField(null=True, blank=True)
    observacao = models.TextField("observação", blank=True)
    resumo_snapshot = models.JSONField("resumo congelado", default=dict, blank=True)

    class Meta:
        ordering = ["-mes"]
        verbose_name = "fechamento mensal"
        verbose_name_plural = "fechamentos mensais"

    def __str__(self) -> str:
        return f"{self.mes:%m/%Y} ({'Fechado' if self.fechado else 'Aberto'})"


class ConfiguracaoSistema(models.Model):
    """Configuração global única (singleton) para identidade visual do sistema."""

    NOME_PADRAO = "OLPI"
    SUBTITULO_PADRAO = "Orçamento, Lançamentos, Patrimônio e Investimentos"
    CICLO_SEGUINTE = "seguinte"
    CICLO_ATUAL = "atual"
    REFERENCIAS_CICLO = [
        (CICLO_SEGUINTE, "Mês seguinte (ex.: 25/10 a 24/11 = Novembro)"),
        (CICLO_ATUAL, "Mês de início (ex.: 05/11 a 04/12 = Novembro)"),
    ]

    nome_sistema = models.CharField(
        "Nome do Sistema",
        max_length=60,
        default=NOME_PADRAO,
    )
    subtitulo_sistema = models.CharField(
        "Subtítulo / Descrição",
        max_length=150,
        blank=True,
        default=SUBTITULO_PADRAO,
    )
    dia_inicio_ciclo = models.PositiveSmallIntegerField(
        "dia de início do ciclo mensal",
        default=1,
        validators=DIA_DO_MES,
        help_text="Use 1 para mês calendário (01 a 31). Ex.: 25 para iniciar no dia em que o salário cai.",
    )
    referencia_ciclo = models.CharField(
        "mês de referência do ciclo",
        max_length=10,
        choices=REFERENCIAS_CICLO,
        default=CICLO_SEGUINTE,
        help_text="Quando o ciclo não começa no dia 1, define qual mês dá nome ao período.",
    )

    class Meta:
        verbose_name = "Configuração do Sistema"
        verbose_name_plural = "Configuração do Sistema"
        constraints = [
            models.CheckConstraint(
                condition=Q(dia_inicio_ciclo__gte=1, dia_inicio_ciclo__lte=31),
                name="config_dia_inicio_ciclo_valido",
            ),
        ]

    def __str__(self) -> str:
        return self.nome_sistema or self.NOME_PADRAO

    def save(self, *args, **kwargs) -> None:
        from django.core.cache import cache

        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete("config_ciclo_financeiro")

    @classmethod
    def carregar(cls) -> ConfiguracaoSistema:
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "nome_sistema": cls.NOME_PADRAO,
                "subtitulo_sistema": cls.SUBTITULO_PADRAO,
            },
        )
        return obj
