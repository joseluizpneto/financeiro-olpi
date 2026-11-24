from django.contrib import admin
from django.db import transaction

from .audit import registrar_auditoria
from .models import (
    Abastecimento, Cartao, Categoria, CentroCusto, Conta, Fatura, FechamentoMensal,
    Integracao, Lancamento, Manutencao, MetaFinanceira, Orcamento, Recorrencia,
    RegistroAuditoria, RegraCategorizacao, TentativaLogin, Titular, TokenAPI, Veiculo,
)


@admin.register(Titular)
class TitularAdmin(admin.ModelAdmin):
    list_display = ["nome", "tipo", "usuario", "ativo"]
    list_select_related = ["usuario"]


@admin.register(CentroCusto)
class CentroCustoAdmin(admin.ModelAdmin):
    list_display = ["nome", "empresa", "codigo_sistema", "ativo"]


@admin.register(Categoria)
class CategoriaAdmin(admin.ModelAdmin):
    list_display = ["__str__", "tipo", "imposto", "codigo_sistema", "ativo"]
    list_filter = ["tipo", "imposto", "ativo"]
    list_select_related = ["pai"]
    search_fields = ["nome"]


@admin.register(Conta)
class ContaAdmin(admin.ModelAdmin):
    list_display = ["nome", "banco", "titular", "tipo", "saldo_inicial", "data_saldo_inicial", "ativo"]
    list_filter = ["titular", "ativo"]
    list_select_related = ["titular", "centro_padrao"]


@admin.register(Cartao)
class CartaoAdmin(admin.ModelAdmin):
    list_display = ["nome", "titular", "limite", "dia_fechamento", "dia_vencimento", "ativo"]
    list_select_related = ["titular", "conta_pagamento", "centro_padrao"]


@admin.register(Fatura)
class FaturaAdmin(admin.ModelAdmin):
    list_display = ["cartao", "referencia", "fechamento", "vencimento", "paga"]
    list_filter = ["cartao", "paga"]
    list_select_related = ["cartao"]


@admin.register(Lancamento)
class LancamentoAdmin(admin.ModelAdmin):
    list_display = [
        "data", "tipo", "descricao", "valor", "pago", "conciliado", "conta", "cartao", "categoria", "centro"
    ]
    list_filter = ["tipo", "pago", "conciliado", "conta", "cartao", "centro"]
    list_select_related = ["conta", "conta_destino", "cartao", "categoria__pai", "centro"]
    search_fields = ["descricao", "observacao"]
    date_hierarchy = "data"
    readonly_fields = [
        "fatura", "recorrencia", "grupo_parcelas", "parcela", "total_parcelas",
        "conciliado_em", "hash_importacao", "criado_por", "atualizado_por", "criado_em", "atualizado_em",
    ]

    def save_model(self, request, obj, form, change):
        if not change and not obj.criado_por_id:
            obj.criado_por = request.user
        obj.atualizado_por = request.user
        super().save_model(request, obj, form, change)
        registrar_auditoria(
            request.user,
            RegistroAuditoria.EDITAR if change else RegistroAuditoria.CRIAR,
            obj,
            f"Admin Django: {obj.descricao} (R$ {obj.valor})",
            request=request,
        )

    @transaction.atomic
    def delete_model(self, request, obj):
        registrar_auditoria(
            request.user,
            RegistroAuditoria.EXCLUIR,
            obj,
            f"Admin Django excluiu: {obj.descricao} (R$ {obj.valor})",
            request=request,
        )
        super().delete_model(request, obj)

    @transaction.atomic
    def delete_queryset(self, request, queryset):
        ids = set()
        for fat_id, fat_pg_id in queryset.values_list("fatura_id", "fatura_paga_id"):
            if fat_id:
                ids.add(fat_id)
            if fat_pg_id:
                ids.add(fat_pg_id)
        total = queryset.count()
        super().delete_queryset(request, queryset)
        Lancamento._atualizar_faturas(ids)
        registrar_auditoria(
            request.user,
            RegistroAuditoria.EXCLUIR,
            "lancamento",
            f"Admin Django excluiu em lote {total} lançamento(s)",
            request=request,
        )


@admin.register(Recorrencia)
class RecorrenciaAdmin(admin.ModelAdmin):
    list_display = ["descricao", "tipo", "valor", "intervalo_meses", "dia", "ativo"]
    list_select_related = ["conta", "cartao", "categoria", "centro"]


@admin.register(Veiculo)
class VeiculoAdmin(admin.ModelAdmin):
    list_display = ["nome", "modelo", "placa", "ativo"]
    list_select_related = ["centro"]


@admin.register(Abastecimento)
class AbastecimentoAdmin(admin.ModelAdmin):
    list_display = ["data", "veiculo", "km", "litros", "valor_total", "tanque_cheio"]
    list_select_related = ["veiculo"]


@admin.register(Manutencao)
class ManutencaoAdmin(admin.ModelAdmin):
    list_display = ["data", "veiculo", "servico", "km", "valor", "proxima_km", "proxima_data"]
    list_select_related = ["veiculo"]


@admin.register(Orcamento)
class OrcamentoAdmin(admin.ModelAdmin):
    list_display = ["categoria", "centro", "valor_mensal", "vigencia_inicio", "vigencia_fim", "ativo"]
    list_select_related = ["categoria__pai", "centro"]


@admin.register(RegistroAuditoria)
class RegistroAuditoriaAdmin(admin.ModelAdmin):
    list_display = ["criado_em", "usuario", "acao", "modelo", "objeto_id", "resumo", "ip"]
    list_filter = ["acao", "modelo"]
    list_select_related = ["usuario"]
    search_fields = ["resumo", "objeto_id"]
    readonly_fields = ["criado_em", "usuario", "acao", "modelo", "objeto_id", "resumo", "detalhes", "ip"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Integracao)
class IntegracaoAdmin(admin.ModelAdmin):
    list_display = ["provedor", "ativo", "atualizado_por", "atualizado_em"]
    list_select_related = ["atualizado_por"]


@admin.register(TokenAPI)
class TokenAPIAdmin(admin.ModelAdmin):
    list_display = ["nome", "usuario", "prefixo", "ativo", "ultimo_uso", "criado_em"]
    list_select_related = ["usuario"]
    readonly_fields = ["prefixo", "token_hash", "ultimo_uso", "criado_em"]


@admin.register(TentativaLogin)
class TentativaLoginAdmin(admin.ModelAdmin):
    list_display = ["chave", "ip", "username", "falhas", "bloqueado_ate", "atualizado_em"]


@admin.register(RegraCategorizacao)
class RegraCategorizacaoAdmin(admin.ModelAdmin):
    list_display = ["prioridade", "padrao", "tipo", "categoria", "centro", "ativo"]
    list_select_related = ["categoria__pai", "centro"]


@admin.register(MetaFinanceira)
class MetaFinanceiraAdmin(admin.ModelAdmin):
    list_display = ["nome", "valor_alvo", "valor_inicial", "prazo", "conta", "ativo"]
    list_select_related = ["conta"]


@admin.register(FechamentoMensal)
class FechamentoMensalAdmin(admin.ModelAdmin):
    list_display = ["mes", "fechado", "fechado_por", "fechado_em"]
    list_select_related = ["fechado_por"]
