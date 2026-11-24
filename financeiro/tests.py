from __future__ import annotations

import io
import json
import os
import tempfile
import urllib.error
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.forms import modelform_factory
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from . import services as sv
from .forms import (
    AbastecimentoForm, ConfigForm, DinheiroInput, LancamentoForm, PagamentoFaturaForm,
    formatar_dinheiro_brl, normalizar_dinheiro_brl,
)
from .importacao import analisar_arquivo_extrato, confirmar_importacao
from .models import (
    Abastecimento, Cartao, Categoria, CentroCusto, Conta, Fatura, FechamentoMensal,
    Integracao, Lancamento, Manutencao, MetaFinanceira, Orcamento, Recorrencia,
    RegistroAuditoria, RegraCategorizacao, TentativaLogin, Titular, TokenAPI, Veiculo,
)
from .notificacoes import enviar_alertas
from .security import csv_seguro
from .utils import hoje

D = Decimal
User = get_user_model()


class Base(TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"SEED_EMPRESA_NOME": "Empresa"}):
            call_command("seed_inicial", verbosity=0)
        pfs = list(Titular.objects.filter(tipo="PF").order_by("nome"))
        self.titular1 = pfs[0]
        self.titular2 = pfs[1]
        self.empresa_tit = Titular.objects.get(tipo="PJ")
        self.casa = CentroCusto.objects.get(codigo_sistema="casa")
        self.empresa = CentroCusto.objects.get(empresa=True)
        self.conta = Conta.objects.create(
            nome="Nubank", titular=self.titular1, saldo_inicial=D("1000"), data_saldo_inicial=date(2026, 1, 1)
        )
        self.conta2 = Conta.objects.create(
            nome="Inter", titular=self.titular1, data_saldo_inicial=date(2026, 1, 1)
        )
        self.conta_pj = Conta.objects.create(
            nome="PJ", titular=self.empresa_tit, centro_padrao=self.empresa, data_saldo_inicial=date(2026, 1, 1)
        )
        self.cartao = Cartao.objects.create(
            nome="Visa",
            titular=self.titular1,
            dia_fechamento=5,
            dia_vencimento=12,
            conta_pagamento=self.conta,
            limite=D("5000"),
        )
        self.mercado = Categoria.objects.get(nome="Supermercado")
        self.salario = Categoria.objects.get(nome="Salário")
        self.das = Categoria.objects.get(nome="DAS / Simples Nacional")
        self.servicos = Categoria.objects.get(nome="Serviços prestados")


class FaturaTests(Base):
    def test_compra_antes_do_fechamento(self):
        f = self.cartao.fatura_para(date(2026, 10, 4))
        self.assertEqual((f.referencia, f.fechamento, f.vencimento), (date(2026, 10, 1), date(2026, 10, 5), date(2026, 10, 12)))

    def test_compra_no_dia_do_fechamento_vai_para_proxima(self):
        f = self.cartao.fatura_para(date(2026, 10, 5))
        self.assertEqual((f.fechamento, f.vencimento), (date(2026, 11, 5), date(2026, 11, 12)))

    def test_vencimento_no_mes_seguinte_ao_fechamento(self):
        c = Cartao.objects.create(nome="Master", titular=self.titular1, dia_fechamento=25, dia_vencimento=5)
        f = c.fatura_para(date(2026, 10, 10))
        self.assertEqual((f.fechamento, f.vencimento), (date(2026, 10, 25), date(2026, 11, 5)))
        f2 = c.fatura_para(date(2026, 10, 26))
        self.assertEqual((f2.fechamento, f2.vencimento), (date(2026, 11, 25), date(2026, 12, 5)))

    def test_dia_31_em_fevereiro(self):
        c = Cartao.objects.create(nome="X", titular=self.titular1, dia_fechamento=31, dia_vencimento=10)
        f = c.fatura_para(date(2027, 2, 15))
        self.assertEqual(f.fechamento, date(2027, 2, 28))

    def test_parcelado(self):
        base = Lancamento(tipo="D", descricao="TV", valor=D("1000.00"), data=date(2026, 10, 4), cartao=self.cartao, categoria=self.mercado)
        itens = sv.criar_parcelado(base, 3)
        self.assertEqual([i.valor for i in itens], [D("333.34"), D("333.33"), D("333.33")])
        self.assertEqual([i.fatura.referencia.month for i in itens], [10, 11, 12])
        self.assertEqual(len({i.grupo_parcelas for i in itens}), 1)
        self.assertEqual(self.cartao.limite_utilizado(), D("1000.00"))

    def test_parcelado_mes_curto_nao_pula_nem_duplica_fatura(self):
        c = Cartao.objects.create(nome="Curto29", titular=self.titular1, dia_fechamento=29, dia_vencimento=10)
        base = Lancamento(tipo="D", descricao="Notebook", valor=D("900.00"), data=date(2027, 1, 28), cartao=c)
        itens = sv.criar_parcelado(base, 3)
        self.assertEqual(
            [i.fatura.referencia for i in itens],
            [date(2027, 2, 1), date(2027, 3, 1), date(2027, 4, 1)],
        )
        c31 = Cartao.objects.create(nome="Curto31", titular=self.titular1, dia_fechamento=31, dia_vencimento=10)
        base2 = Lancamento(tipo="D", descricao="Celular", valor=D("1200.00"), data=date(2026, 3, 30), cartao=c31)
        itens2 = sv.criar_parcelado(base2, 3)
        self.assertEqual(
            [i.fatura.referencia for i in itens2],
            [date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1)],
        )

    def test_pagamento_quita_fatura_e_baixa_saldo(self):
        compra = Lancamento.objects.create(tipo="D", descricao="Mercado", valor=D("200"), data=date(2026, 10, 1), cartao=self.cartao, categoria=self.mercado)
        f = compra.fatura
        self.assertEqual(sv.saldos_contas([self.conta])[0].saldo_atual, D("1000"))
        pg = Lancamento.objects.create(tipo="F", descricao="Pgto", valor=D("200"), data=date(2026, 10, 12), conta=self.conta, fatura_paga=f)
        f.refresh_from_db()
        self.assertTrue(f.paga)
        self.assertEqual(sv.saldos_contas([self.conta])[0].saldo_atual, D("800"))
        pg.delete()
        f.refresh_from_db()
        self.assertFalse(f.paga)

    def test_editar_data_move_de_fatura(self):
        l = Lancamento.objects.create(tipo="D", descricao="X", valor=D("50"), data=date(2026, 10, 1), cartao=self.cartao)
        self.assertEqual(l.fatura.referencia, date(2026, 10, 1))
        l.data = date(2026, 10, 20)
        l.save()
        self.assertEqual(l.fatura.referencia, date(2026, 11, 1))

    def test_pagamento_um_centavo_excedente_e_duplicado(self):
        form = PagamentoFaturaForm(data={"conta": self.conta.pk, "valor": "0.01", "data": "2026-10-12"})
        self.assertTrue(form.is_valid(), form.errors)

        compra = Lancamento.objects.create(tipo="D", descricao="Café", valor=D("10.00"), data=date(2026, 10, 1), cartao=self.cartao)
        f = compra.fatura
        with self.assertRaises(ValidationError):
            sv.pagar_fatura(f.pk, conta=self.conta, valor=D("15.00"), data=date(2026, 10, 12))
        sv.pagar_fatura(f.pk, conta=self.conta, valor=D("10.00"), data=date(2026, 10, 12))
        with self.assertRaises(ValidationError):
            sv.pagar_fatura(f.pk, conta=self.conta, valor=D("1.00"), data=date(2026, 10, 12))

    def test_estorno_total_quita_fatura_e_nao_infla_receita(self):
        compra = Lancamento.objects.create(
            tipo="D", descricao="Compra", valor=D("200.00"), data=date(2026, 10, 1), cartao=self.cartao, categoria=self.mercado
        )
        Lancamento.objects.create(
            tipo="E", descricao="Estorno compra", valor=D("200.00"), data=date(2026, 10, 2), cartao=self.cartao, categoria=self.mercado
        )
        f = compra.fatura
        f.refresh_from_db()
        self.assertEqual(f.total, D("0.00"))
        self.assertTrue(f.paga)

        qs = sv.lancamentos_do_mes(date(2026, 10, 1))
        self.assertEqual(sv.soma(qs.filter(tipo="R")), D("0.00"))
        self.assertEqual(sv.despesas_liquidas(qs), D("0.00"))
        self.assertEqual(list(sv.despesas_por_categoria(qs)), [])


class SaldoTests(Base):
    def test_receita_despesa_transferencia(self):
        Lancamento.objects.create(tipo="R", descricao="Salário", valor=D("3000"), data=date(2026, 10, 1), conta=self.conta, categoria=self.salario)
        Lancamento.objects.create(tipo="D", descricao="Luz", valor=D("150"), data=date(2026, 10, 1), conta=self.conta)
        Lancamento.objects.create(tipo="D", descricao="Futuro", valor=D("999"), data=date(2026, 10, 1), conta=self.conta, pago=False)
        Lancamento.objects.create(tipo="T", descricao="Reserva", valor=D("500"), data=date(2026, 10, 1), conta=self.conta, conta_destino=self.conta2)
        saldos = {c.nome: c.saldo_atual for c in sv.saldos_contas()}
        self.assertEqual(saldos["Nubank"], D("3350"))
        self.assertEqual(saldos["Inter"], D("500"))

    def test_data_saldo_inicial_e_futuro(self):
        c = Conta.objects.create(
            nome="Caixa Novo", titular=self.titular1, saldo_inicial=D("500.00"), data_saldo_inicial=date(2026, 11, 10)
        )
        # Antes da data inicial: deve ser ignorado no saldo
        Lancamento.objects.create(tipo="R", descricao="Antigo", valor=D("200.00"), data=date(2026, 11, 5), conta=c)
        # Na data inicial ou depois até hoje
        Lancamento.objects.create(tipo="D", descricao="Atual", valor=D("50.00"), data=date(2026, 11, 11), conta=c)
        # Pago com data futura além de `ate`
        Lancamento.objects.create(tipo="R", descricao="Futuro pago", valor=D("300.00"), data=date(2099, 1, 1), conta=c)
        self.assertEqual(sv.saldos_contas([c], ate=date(2026, 11, 9))[0].saldo_atual, D("0.00"))
        self.assertEqual(sv.saldos_contas([c], ate=date(2026, 11, 15))[0].saldo_atual, D("450.00"))

    def test_centro_padrao_da_conta(self):
        l = Lancamento.objects.create(tipo="R", descricao="Cliente", valor=D("10"), conta=self.conta_pj)
        self.assertEqual(l.centro, self.empresa)

    def test_transferencia_nao_conta_como_despesa(self):
        Lancamento.objects.create(tipo="T", descricao="Pró-labore", valor=D("500"), conta=self.conta_pj, conta_destino=self.conta)
        qs = sv.lancamentos_do_mes(hoje().replace(day=1))
        self.assertEqual(sv.soma(qs.filter(tipo="D")), 0)

    def test_estorno_em_conta_devolve_saldo_sem_inflar_receita(self):
        Lancamento.objects.create(tipo="D", descricao="Compra", valor=D("100"), data=date(2026, 10, 5), conta=self.conta, categoria=self.mercado)
        Lancamento.objects.create(tipo="E", descricao="Reembolso", valor=D("40"), data=date(2026, 10, 6), conta=self.conta, categoria=self.mercado)
        self.assertEqual(sv.saldos_contas([self.conta], ate=date(2026, 10, 31))[0].saldo_atual, D("940.00"))
        qs = sv.lancamentos_do_mes(date(2026, 10, 1))
        self.assertEqual(sv.soma(qs.filter(tipo="R")), D("0.00"))
        self.assertEqual(sv.despesas_liquidas(qs), D("60.00"))


class RecorrenciaTests(Base):
    def test_gera_sem_duplicar(self):
        r = Recorrencia.objects.create(descricao="Internet", tipo="D", valor=D("120"), conta=self.conta, dia=10, inicio=date(2026, 10, 10))
        self.assertEqual(r.gerar(date(2027, 1, 15)), 4)
        self.assertEqual(r.gerar(date(2027, 1, 15)), 0)
        self.assertEqual(Lancamento.objects.filter(recorrencia=r, pago=False).count(), 4)

    def test_dia_31_e_anual(self):
        r = Recorrencia.objects.create(descricao="Aluguel", tipo="D", valor=D("1"), conta=self.conta, dia=31, inicio=date(2027, 1, 1))
        datas = list(r.datas(date(2027, 3, 31)))
        self.assertEqual(datas, [date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31)])
        ipva = Recorrencia.objects.create(descricao="IPVA", tipo="D", valor=D("1"), conta=self.conta, dia=15, inicio=date(2027, 2, 1), intervalo_meses=12)
        self.assertEqual(list(ipva.datas(date(2029, 12, 31))), [date(2027, 2, 15), date(2028, 2, 15), date(2029, 2, 15)])

    def test_recorrencia_no_cartao_vai_para_fatura(self):
        r = Recorrencia.objects.create(descricao="Streaming", tipo="D", valor=D("40"), cartao=self.cartao, dia=20, inicio=date(2026, 10, 1))
        r.gerar(date(2026, 10, 31))
        l = Lancamento.objects.get(recorrencia=r)
        self.assertTrue(l.pago)
        self.assertEqual(l.fatura.referencia, date(2026, 11, 1))

    @patch("financeiro.services.hoje", return_value=date(2026, 10, 1))
    @patch("financeiro.models.hoje", return_value=date(2026, 10, 1))
    def test_desativar_reativar_e_mudar_dia_sem_perder_nem_duplicar(self, *_mocks):
        r = Recorrencia.objects.create(descricao="Escola", tipo="D", valor=D("800"), conta=self.conta, dia=10, inicio=date(2026, 10, 1))
        r.gerar(date(2026, 12, 31))
        self.assertEqual(Lancamento.objects.filter(recorrencia=r).count(), 3)

        # Desativar apaga futuras e reseta ultima_gerada
        r.ativo = False
        r.save()
        sv.sincronizar_recorrencia(r)
        self.assertEqual(Lancamento.objects.filter(recorrencia=r).count(), 0)
        self.assertIsNone(r.ultima_gerada)

        # Reativar regenera sem perda
        r.ativo = True
        r.save()
        self.assertEqual(r.gerar(date(2026, 12, 31)), 3)

        # Alterar dia atualiza datas existentes sem duplicar
        r.dia = 15
        r.save()
        sv.sincronizar_recorrencia(r)
        self.assertEqual(r.gerar(date(2026, 12, 31)), 0)
        self.assertEqual(
            list(Lancamento.objects.filter(recorrencia=r).order_by("data").values_list("data", flat=True)),
            [date(2026, 10, 15), date(2026, 11, 15), date(2026, 12, 15)],
        )


class ConstraintsEIntegridadeTests(Base):
    def test_check_constraints_e_unicidade_null(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Lancamento.objects.create(tipo="D", descricao="Zero", valor=D("0.00"), conta=self.conta)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Lancamento.objects.create(tipo="T", descricao="Mesma", valor=D("10.00"), conta=self.conta, conta_destino=self.conta)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Categoria.objects.create(nome="Alimentação", tipo="D", pai=None)

        alim = Categoria.objects.get(nome="Alimentação")
        Orcamento.objects.create(categoria=alim, centro=None, valor_mensal=D("1500"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            Orcamento.objects.create(categoria=alim, centro=None, valor_mensal=D("2000"))

    def test_orcamento_vigencia_e_sem_dupla_contagem_pai_filha(self):
        alim = Categoria.objects.get(nome="Alimentação")
        Orcamento.objects.create(
            categoria=alim, valor_mensal=D("1000"), vigencia_inicio=date(2026, 1, 1), vigencia_fim=date(2026, 6, 30)
        )
        Orcamento.objects.create(
            categoria=self.mercado, valor_mensal=D("500"), vigencia_inicio=date(2026, 1, 1)
        )
        Lancamento.objects.create(tipo="D", descricao="Feira", valor=D("300"), data=date(2026, 5, 10), conta=self.conta, categoria=self.mercado)
        itens_maio = sv.orcamento_mes(date(2026, 5, 1))
        self.assertEqual(len(itens_maio), 2)
        lim, gasto = sv.totais_orcamento(itens_maio)
        self.assertEqual(lim, D("1000"))
        self.assertEqual(gasto, D("300"))

        itens_julho = sv.orcamento_mes(date(2026, 7, 1))
        self.assertEqual(len(itens_julho), 1)
        self.assertEqual(itens_julho[0]["o"].categoria, self.mercado)


class VeiculoTests(Base):
    def setUp(self):
        super().setUp()
        self.v = Veiculo.objects.create(nome="Carro", km_inicial=900)

    def test_consumo(self):
        Abastecimento.objects.create(veiculo=self.v, data=date(2026, 10, 1), km=1000, litros=D("40"), valor_total=D("240"), tanque_cheio=True)
        Abastecimento.objects.create(veiculo=self.v, data=date(2026, 10, 8), km=1500, litros=D("20"), valor_total=D("120"), tanque_cheio=False)
        Abastecimento.objects.create(veiculo=self.v, data=date(2026, 10, 15), km=1900, litros=D("15"), valor_total=D("90"), tanque_cheio=True)
        itens, media = sv.consumo_abastecimentos(self.v)
        self.assertEqual(media, D("25.71"))
        self.assertEqual(itens[0].km_l, D("25.71"))
        self.assertEqual(self.v.km_atual(), 1900)

    def test_alerta_manutencao(self):
        Abastecimento.objects.create(veiculo=self.v, km=10000, litros=D("10"), valor_total=D("60"))
        Manutencao.objects.create(veiculo=self.v, servico="Troca de óleo", km=5000, proxima_km=10200)
        alertas = sv.alertas_manutencao(self.v)
        self.assertEqual(len(alertas), 1)
        self.assertFalse(alertas[0]["vencido"])
        Manutencao.objects.create(veiculo=self.v, servico="troca de óleo", km=10000, proxima_km=15000)
        self.assertEqual(sv.alertas_manutencao(self.v), [])

    def test_categoria_veiculo_resiste_a_renomeacao(self):
        cat_veic = Categoria.objects.get(codigo_sistema="veiculo")
        cat_veic.nome = "Automóvel"
        cat_veic.save()
        cat_comb = sv.categoria_veiculo("Combustível")
        self.assertEqual(cat_comb.codigo_sistema, "combustivel")


class PaginasTests(Base):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser("admin_teste", "admin@example.com", "senha-forte-123")
        self.client.force_login(self.user)
        self.v = Veiculo.objects.create(nome="Carro")

    def test_login_obrigatorio(self):
        self.client.logout()
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)

    def test_fluxo_completo(self):
        c = self.client
        # despesa à vista
        r = c.post(reverse("lancamento_novo"), {"tipo": "D", "descricao": "Mercado", "valor": "250.50", "data": hoje().isoformat(), "forma": "conta", "conta": self.conta.pk, "categoria": self.mercado.pk, "centro": self.casa.pk, "pago": "on"})
        self.assertEqual(r.status_code, 302, r.content[:3000])
        # compra parcelada no cartão
        r = c.post(reverse("lancamento_novo"), {"tipo": "D", "descricao": "Geladeira", "valor": "3000", "data": hoje().isoformat(), "forma": "cartao", "cartao": self.cartao.pk, "parcelas": "10", "categoria": self.mercado.pk})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Lancamento.objects.filter(descricao="Geladeira").count(), 10)
        # receita da empresa: centro preenchido pela conta
        c.post(reverse("lancamento_novo"), {"tipo": "R", "descricao": "Cliente A", "valor": "5000", "data": hoje().isoformat(), "forma": "conta", "conta": self.conta_pj.pk, "categoria": self.servicos.pk, "pago": "on"})
        self.assertEqual(Lancamento.objects.get(descricao="Cliente A").centro, self.empresa)
        # imposto pendente
        c.post(reverse("lancamento_novo"), {"tipo": "D", "descricao": "DAS", "valor": "300", "data": hoje().isoformat(), "forma": "conta", "conta": self.conta_pj.pk, "categoria": self.das.pk, "vencimento": (hoje() + timedelta(days=3)).isoformat()})
        das = Lancamento.objects.get(descricao="DAS")
        self.assertFalse(das.pago)
        # transferência
        r = c.post(reverse("lancamento_novo"), {"tipo": "T", "descricao": "Pró-labore", "valor": "1000", "data": hoje().isoformat(), "conta": self.conta_pj.pk, "conta_destino": self.conta.pk, "pago": "on"})
        self.assertEqual(r.status_code, 302)
        # transferência pendente não entra em a_pagar na tela A vencer
        Lancamento.objects.create(tipo="T", descricao="Transf agendada", valor=D("777"), data=hoje(), vencimento=hoje() + timedelta(days=2), conta=self.conta, conta_destino=self.conta2, pago=False)
        r_av = c.get(reverse("a_vencer"))
        self.assertNotEqual(r_av.context["a_pagar"], D("777") + D("300"))
        # validação: despesa sem conta
        r = c.post(reverse("lancamento_novo"), {"tipo": "D", "descricao": "X", "valor": "10", "data": hoje().isoformat(), "forma": "conta"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Escolha a conta")
        # recorrência
        r = c.post(reverse("recorrencia_nova"), {"tipo": "D", "descricao": "Internet", "valor": "120", "intervalo_meses": "1", "dia": "15", "forma": "conta", "conta": self.conta.pk, "inicio": hoje().isoformat(), "ativo": "on"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Lancamento.objects.filter(descricao="Internet").exists())
        # abastecimento gera lançamento
        r = c.post(reverse("abastecimento_novo", args=[self.v.pk]), {"data": hoje().isoformat(), "km": "1000", "litros": "40", "valor_total": "240", "combustivel": "gasolina", "tanque_cheio": "on", "forma": "cartao", "cartao": self.cartao.pk})
        self.assertEqual(r.status_code, 302)
        a = Abastecimento.objects.get()
        self.assertEqual(a.lancamento.veiculo, self.v)
        self.assertEqual(a.lancamento.categoria.nome, "Combustível")
        # editar lançamento de abastecimento redireciona para tela do veículo
        r_edit_ab = c.get(reverse("lancamento_editar", args=[a.lancamento.pk]))
        self.assertEqual(r_edit_ab.status_code, 302)
        # manutenção sem valor não exige conta
        r = c.post(reverse("manutencao_nova", args=[self.v.pk]), {"data": hoje().isoformat(), "servico": "Calibragem", "valor": "0"})
        self.assertEqual(r.status_code, 302)
        # orçamento
        alimentacao = Categoria.objects.get(nome="Alimentação")
        c.post(reverse("orcamento_novo"), {"categoria": alimentacao.pk, "valor_mensal": "1000", "ativo": "on"})
        itens = sv.orcamento_mes(hoje().replace(day=1))
        self.assertEqual(itens[0]["gasto"], D("550.50"))  # 250,50 à vista + 300 (1ª parcela)
        # marcar DAS como pago
        c.post(reverse("lancamento_pagar", args=[das.pk]))
        das.refresh_from_db()
        self.assertTrue(das.pago)
        # pagar fatura
        f = self.cartao.fatura_para(hoje())
        r = c.post(reverse("fatura_pagar", args=[f.pk]), {"conta": self.conta.pk, "valor": str(f.saldo), "data": hoje().isoformat()})
        f.refresh_from_db()
        self.assertTrue(f.paga)

        # todas as páginas abrem
        urls = [
            reverse("dashboard"), reverse("dashboard") + "?mes=2026-01",
            reverse("dashboard") + "?escopo=pessoal", reverse("fluxo_caixa"),
            reverse("lancamentos"), reverse("lancamentos") + "?mes=todos&tipo=D&q=gel",
            reverse("lancamentos_csv"), reverse("importar_extrato"),
            reverse("a_vencer"), reverse("fechamento_mensal"),
            reverse("cartoes"), reverse("fatura", args=[f.pk]),
            reverse("fatura_mes", args=[self.cartao.pk, "2028-05"]),
            reverse("recorrencias"), reverse("recorrencia_nova"),
            reverse("orcamento"), reverse("orcamento_novo"),
            reverse("metas"), reverse("meta_nova"),
            reverse("veiculo"), reverse("abastecimento_novo", args=[self.v.pk]), reverse("abastecimento_editar", args=[a.pk]),
            reverse("manutencao_nova", args=[self.v.pk]),
            reverse("empresa"), reverse("relatorios"), reverse("rateio"),
            reverse("conta_extrato", args=[self.conta.pk]), reverse("config"),
            reverse("regras_lista"), reverse("regra_nova"),
            reverse("admin_painel"), reverse("admin_usuarios"), reverse("admin_usuario_criar"),
            reverse("admin_integracoes"), reverse("admin_tokens"), reverse("admin_sessoes"), reverse("admin_auditoria"),
            reverse("lancamento_novo"), reverse("lancamento_novo") + "?tipo=R",
            reverse("lancamento_editar", args=[Lancamento.objects.filter(descricao="Geladeira").first().pk]),
            reverse("admin:index"), reverse("password_change"),
        ]
        for slug in ("titulares", "contas", "cartoes", "categorias", "centros", "veiculos"):
            urls += [reverse("config_lista", args=[slug]), reverse("config_novo", args=[slug])]
        urls.append(reverse("config_editar", args=["contas", self.conta.pk]))
        for u in urls:
            resp = c.get(u)
            self.assertEqual(resp.status_code, 200, f"{u} → {resp.status_code}")

        # relatório da empresa
        r = c.get(reverse("empresa"))
        self.assertEqual(r.context["atual"]["entradas"], D("5000"))
        self.assertEqual(r.context["atual"]["impostos"], D("300"))
        self.assertEqual(r.context["atual"]["retiradas"], D("1000"))

        # exclusão de parcelas em grupo
        g = Lancamento.objects.filter(descricao="Geladeira").first()
        c.post(reverse("lancamento_excluir", args=[g.pk]), {"todas": "1"})
        self.assertFalse(Lancamento.objects.filter(descricao="Geladeira").exists())

        # cadastro em uso não pode ser excluído
        r = c.post(reverse("config_excluir", args=["contas", self.conta.pk]))
        self.assertTrue(Conta.objects.filter(pk=self.conta.pk).exists())

    def test_navegacao_fatura_futura_nao_cria_no_banco_e_sem_n1(self):
        qtd_antes = Fatura.objects.count()
        r = self.client.get(reverse("fatura_mes", args=[self.cartao.pk, "2029-08"]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Fatura.objects.count(), qtd_antes)

        # Sem explosão N+1 em cartões e painel mesmo com múltiplos cartões e faturas
        for i in range(4):
            card = Cartao.objects.create(nome=f"C{i}", titular=self.titular1, dia_fechamento=10, dia_vencimento=20, limite=D("3000"))
            for m in range(1, 5):
                Lancamento.objects.create(tipo="D", descricao=f"Compra {i}-{m}", valor=D("50"), data=date(2026, m, 1), cartao=card)
        sv.gerar_recorrencias(forcar=False)
        with CaptureQueriesContext(connection) as ctx_dash:
            r_dash = self.client.get(reverse("dashboard"))
            self.assertEqual(r_dash.status_code, 200)
        self.assertLessEqual(len(ctx_dash), 30)

        with CaptureQueriesContext(connection) as ctx_cards:
            r_cards = self.client.get(reverse("cartoes"))
            self.assertEqual(r_cards.status_code, 200)
        self.assertLessEqual(len(ctx_cards), 8)

    def test_cartoes_sem_faturas_salvas(self):
        Cartao.objects.create(nome="Novo Card", titular=self.titular1, dia_fechamento=5, dia_vencimento=15)
        r = self.client.get(reverse("cartoes"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Novo Card")

    def test_conta_extrato_concilia_com_saldo_inicial_no_meio_do_mes(self):
        c = Conta.objects.create(
            nome="Extrato Meio", titular=self.titular1, saldo_inicial=D("500.00"), data_saldo_inicial=date(2026, 10, 10)
        )
        Lancamento.objects.create(tipo="D", descricao="Antes", valor=D("99.00"), data=date(2026, 10, 5), conta=c)
        Lancamento.objects.create(tipo="D", descricao="Depois", valor=D("100.00"), data=date(2026, 10, 15), conta=c)
        r = self.client.get(reverse("conta_extrato", args=[c.pk]) + "?mes=2026-10")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["saldo_anterior"], D("0.00"))
        self.assertEqual(r.context["saldo_final"], D("400.00"))
        self.assertEqual(r.context["saldo_final"], sv.saldos_contas([c], ate=date(2026, 10, 31))[0].saldo_atual)

    def test_parametros_extremos_nao_geram_500(self):
        for url in (
            reverse("relatorios") + "?ano=99999",
            reverse("relatorios") + "?ano=0",
            reverse("relatorios") + "?ano=abc",
            reverse("dashboard") + "?mes=9999-12",
            reverse("dashboard") + "?mes=0000-00",
        ):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)

    def test_csv_formula_injection_neutralizada(self):
        self.assertEqual(csv_seguro("=SUM(A1:A2)"), "'=SUM(A1:A2)")
        self.assertEqual(csv_seguro("+cmd"), "'+cmd")
        self.assertEqual(csv_seguro("-1+1"), "'-1+1")
        self.assertEqual(csv_seguro("@SUM(1)"), "'@SUM(1)")
        self.assertEqual(csv_seguro("\t=1"), "'\t=1")

        Lancamento.objects.create(tipo="D", descricao="=HYPERLINK(\"http://evil\")", valor=D("15.00"), data=hoje(), conta=self.conta)
        resp = self.client.get(reverse("lancamentos_csv"))
        conteudo = b"".join(resp.streaming_content).decode("utf-8")
        self.assertIn("'=HYPERLINK", conteudo)

    def test_parte_empresarial_oculta_sem_empresa_cadastrada(self):
        self.empresa.ativo = False
        self.empresa.save()

        for url in (reverse("dashboard"), reverse("fluxo_caixa"), reverse("relatorios")):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.context["tem_empresa"])
            self.assertNotContains(r, f'href="{reverse("empresa")}"')
            self.assertNotContains(r, "Empresa (PJ)")

        r_escopo = self.client.get(reverse("dashboard") + "?escopo=empresa")
        self.assertEqual(r_escopo.status_code, 200)
        self.assertEqual(r_escopo.context["escopo"], "todos")

        self.empresa.ativo = True
        self.empresa.save()
        r_ativo = self.client.get(reverse("dashboard"))
        self.assertTrue(r_ativo.context["tem_empresa"])
        self.assertContains(r_ativo, f'href="{reverse("empresa")}"')
        self.assertContains(r_ativo, "Empresa (PJ)")


class SeedInicialTests(TestCase):
    def test_seed_sem_empresa_cria_apenas_pf(self):
        with patch.dict(os.environ, {"SEED_EMPRESA_NOME": "", "SEED_TITULARES": ""}):
            call_command("seed_inicial", verbosity=0)
        self.assertFalse(Titular.objects.filter(tipo=Titular.PJ).exists())
        self.assertFalse(CentroCusto.objects.filter(empresa=True).exists())
        self.assertTrue(CentroCusto.objects.filter(codigo_sistema="casa").exists())

    def test_seed_com_empresa_cria_pj_e_centro(self):
        with patch.dict(os.environ, {"SEED_EMPRESA_NOME": "Minha Empresa", "SEED_TITULARES": ""}):
            call_command("seed_inicial", verbosity=0)
        self.assertTrue(Titular.objects.filter(nome="Minha Empresa", tipo=Titular.PJ).exists())
        self.assertTrue(CentroCusto.objects.filter(nome="Minha Empresa", empresa=True, codigo_sistema="empresa").exists())


class SegurancaEPainelAdminTests(Base):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("admin_teste", "admin@example.com", "SenhaForte#123")

    @override_settings(LOGIN_MAX_TENTATIVAS=3, LOGIN_BLOQUEIO_MINUTOS=10)
    def test_rate_limit_login_e_desbloqueio(self):
        for _ in range(2):
            r = self.client.post(reverse("login"), {"username": "admin_teste", "password": "errada"})
            self.assertEqual(r.status_code, 200)
        # 3a falha dispara bloqueio 429
        r = self.client.post(reverse("login"), {"username": "admin_teste", "password": "errada"})
        self.assertEqual(r.status_code, 429)
        # Mesmo com senha correta continua bloqueado
        r = self.client.post(reverse("login"), {"username": "admin_teste", "password": "SenhaForte#123"})
        self.assertEqual(r.status_code, 429)

        # Limpar bloqueios pelo painel admin autenticado
        self.client.force_login(self.admin)
        self.client.post(reverse("admin_desbloquear_todos"))
        self.assertEqual(TentativaLogin.objects.count(), 0)

        self.client.logout()
        r = self.client.post(reverse("login"), {"username": "admin_teste", "password": "SenhaForte#123"})
        self.assertEqual(r.status_code, 302)

    def test_usuarios_integracoes_tokens_e_auditoria(self):
        self.client.force_login(self.admin)

        # Criar segundo usuário vinculado ao titular 2
        r = self.client.post(
            reverse("admin_usuario_criar"),
            {
                "username": "usuario2",
                "first_name": "Titular 2",
                "password1": "SenhaTeste#99",
                "password2": "SenhaTeste#99",
                "titular": self.titular2.pk,
                "telegram_chat_id": "123456789",
                "is_active": "on",
            },
        )
        self.assertEqual(r.status_code, 302)
        u_sec = User.objects.get(username="usuario2")
        self.titular2.refresh_from_db()
        self.assertEqual(self.titular2.usuario, u_sec)
        self.assertEqual(self.titular2.telegram_chat_id, "123456789")

        # Não pode desativar a si mesmo
        r = self.client.post(
            reverse("admin_usuario_editar", args=[self.admin.pk]),
            {"username": "admin_teste", "is_staff": "on"},  # is_active ausente => False
        )
        self.assertEqual(r.status_code, 200)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

        # Redefinir senha de outro usuário
        r = self.client.post(
            reverse("admin_usuario_senha", args=[u_sec.pk]),
            {"nova_senha1": "NovaSenha#789", "nova_senha2": "NovaSenha#789"},
        )
        self.assertEqual(r.status_code, 302)

        # Salvar integrações Telegram e IA sem vazar segredo em HTML
        self.client.post(
            reverse("admin_integracoes"),
            {"provedor": "telegram", "ativo": "on", "token": "bot123456:SECRET_TOKEN_XYZ", "chat_id": "998877"},
        )
        tg = Integracao.objects.get(provedor=Integracao.TELEGRAM)
        self.assertEqual(tg.segredo, "bot123456:SECRET_TOKEN_XYZ")
        r_int = self.client.get(reverse("admin_integracoes"))
        self.assertNotContains(r_int, "SECRET_TOKEN_XYZ")
        self.assertContains(r_int, "bot1••••_XYZ")

        # Salvar sem preencher token mantém segredo anterior
        self.client.post(
            reverse("admin_integracoes"),
            {"provedor": "telegram", "ativo": "on", "token": "", "chat_id": "111222"},
        )
        tg.refresh_from_db()
        self.assertEqual(tg.segredo, "bot123456:SECRET_TOKEN_XYZ")

        # Token API e consumo do endpoint JSON
        r_tok = self.client.post(reverse("admin_tokens"), {"nome": "Bot Teste", "usuario": u_sec.pk})
        self.assertEqual(r_tok.status_code, 302)
        plano = self.client.session["novo_token_api"]["token"]
        self.assertEqual(self.client.get(reverse("api_resumo")).status_code, 401)
        r_api = self.client.get(reverse("api_resumo"), HTTP_AUTHORIZATION=f"Bearer {plano}")
        self.assertEqual(r_api.status_code, 200)
        payload = json.loads(r_api.content)
        self.assertIn("saldo_total", payload)
        tok = TokenAPI.objects.get(nome="Bot Teste")
        self.client.post(reverse("admin_token_revogar", args=[tok.pk]))
        self.assertEqual(self.client.get(reverse("api_resumo"), HTTP_AUTHORIZATION=f"Bearer {plano}").status_code, 401)

        # Auditoria registra criação, edição (com diff) e exclusão
        self.client.post(
            reverse("lancamento_novo"),
            {"tipo": "D", "descricao": "Farmácia", "valor": "80.00", "data": hoje().isoformat(), "forma": "conta", "conta": self.conta.pk, "pago": "on"},
        )
        lanc = Lancamento.objects.get(descricao="Farmácia")
        self.client.post(
            reverse("lancamento_editar", args=[lanc.pk]),
            {"tipo": "D", "descricao": "Farmácia Pague Menos", "valor": "95.00", "data": hoje().isoformat(), "forma": "conta", "conta": self.conta.pk, "pago": "on"},
        )
        log_edit = RegistroAuditoria.objects.filter(modelo="lancamento", acao=RegistroAuditoria.EDITAR, objeto_id=str(lanc.pk)).first()
        self.assertIsNotNone(log_edit)
        self.assertEqual(log_edit.detalhes["alteracoes"]["valor"], {"antes": "80.00", "depois": "95.00"})
        self.client.post(reverse("lancamento_excluir", args=[lanc.pk]))
        self.assertTrue(RegistroAuditoria.objects.filter(modelo="lancamento", acao=RegistroAuditoria.EXCLUIR, objeto_id=str(lanc.pk)).exists())


class FluxosFinanceirosTests(Base):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser("admin_teste", "admin@example.com", "SenhaForte#123")
        self.client.force_login(self.user)

    def test_importacao_ofx_csv_e_deduplicacao(self):
        RegraCategorizacao.objects.create(padrao="IFOOD", tipo="D", categoria=self.mercado, centro=self.casa, prioridade=1)
        ofx = b"""<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261015<TRNAMT>-45.90<FITID>FIT001<MEMO>IFOOD *RESTAURANTE</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261016<TRNAMT>500.00<FITID>FIT002<MEMO>PIX CLIENTE</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""
        arq = SimpleUploadedFile("extrato.ofx", ofx, content_type="application/octet-stream")
        itens = analisar_arquivo_extrato(arq, conta=self.conta)
        self.assertEqual(len(itens), 2)
        self.assertEqual(itens[0]["categoria_id"], self.mercado.pk)
        criados, ignorados = confirmar_importacao(itens, conta=self.conta, usuario=self.user)
        self.assertEqual((criados, ignorados), (2, 0))

        # Reimportar o mesmo arquivo detecta duplicados
        arq2 = SimpleUploadedFile("extrato.ofx", ofx, content_type="application/octet-stream")
        itens2 = analisar_arquivo_extrato(arq2, conta=self.conta)
        self.assertTrue(all(i["ja_existe"] for i in itens2))
        criados2, ignorados2 = confirmar_importacao(itens2, conta=self.conta, usuario=self.user)
        self.assertEqual((criados2, ignorados2), (0, 2))

    def test_fluxo_caixa_fechamento_bloqueio_rateio_metas_e_comprovante(self):
        # Fechamento mensal bloqueia edições
        self.client.post(reverse("fechamento_mensal") + "?mes=2026-09", {"acao": "fechar", "observacao": "Tudo conferido"})
        self.assertTrue(FechamentoMensal.objects.filter(mes=date(2026, 9, 1), fechado=True).exists())
        r = self.client.post(
            reverse("lancamento_novo"),
            {"tipo": "D", "descricao": "Bloqueado", "valor": "50", "data": "2026-09-10", "forma": "conta", "conta": self.conta.pk, "pago": "on"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "fechado")

        # Conta bloqueada até 2026-08-31 bloqueia edição
        self.conta.bloqueado_ate = date(2026, 8, 31)
        self.conta.save()
        self.assertIsNotNone(sv.verificar_bloqueio_lancamento(date(2026, 8, 15), conta=self.conta))

        # Fluxo de caixa projetado detecta saldo negativo futuro
        Lancamento.objects.create(
            tipo="D", descricao="Boleto gigante", valor=D("5000.00"),
            data=hoje(), vencimento=hoje() + timedelta(days=5), conta=self.conta, pago=False,
        )
        proj = sv.fluxo_caixa_projetado(dias=30)
        self.assertTrue(proj["fica_negativo"])
        self.assertEqual(proj["saldo_minimo"], D("-4000.00"))

        # Rateio mensal 50/50
        Lancamento.objects.create(
            tipo="D", descricao="Mercado casa", valor=D("600.00"),
            data=date(2026, 10, 10), conta=self.conta, centro=self.casa, compartilhado=True, pago_por=self.titular1,
        )
        rat = sv.rateio_mensal(date(2026, 10, 1), modo="igual")
        self.assertEqual(rat["total"], D("600.00"))
        self.assertEqual(len(rat["acertos"]), 1)
        self.assertEqual((rat["acertos"][0]["de"], rat["acertos"][0]["para"], rat["acertos"][0]["valor"]), (self.titular2, self.titular1, D("300.00")))

        # Metas com conta vinculada (saldo de self.conta já é 1000 - 600 = 400 + 500 inicial = 900)
        MetaFinanceira.objects.create(nome="Reserva", valor_alvo=D("2000.00"), valor_inicial=D("500.00"), conta=self.conta)
        metas = sv.resumo_metas()
        self.assertEqual(metas[0]["atual"], D("900.00"))
        self.assertEqual(metas[0]["pct"], 45)

        # Comprovante: rejeita extensão inválida e protege download autenticado
        with tempfile.TemporaryDirectory() as tmp_media, override_settings(MEDIA_ROOT=tmp_media):
            exe = SimpleUploadedFile("virus.exe", b"MZ", content_type="application/octet-stream")
            r_bad = self.client.post(
                reverse("lancamento_novo"),
                {"tipo": "D", "descricao": "Comprov", "valor": "10", "data": hoje().isoformat(), "forma": "conta", "conta": self.conta.pk, "pago": "on", "comprovante": exe},
            )
            self.assertEqual(r_bad.status_code, 200)
            self.assertContains(r_bad, "Formato não permitido")

            pdf = SimpleUploadedFile("recibo.pdf", b"%PDF-1.4\n%", content_type="application/pdf")
            r_ok = self.client.post(
                reverse("lancamento_novo"),
                {"tipo": "D", "descricao": "Comprov OK", "valor": "10", "data": hoje().isoformat(), "forma": "conta", "conta": self.conta.pk, "pago": "on", "comprovante": pdf},
            )
            self.assertEqual(r_ok.status_code, 302)
            l_pdf = Lancamento.objects.get(descricao="Comprov OK")
            self.assertEqual(self.client.get(reverse("lancamento_comprovante", args=[l_pdf.pk])).status_code, 200)
            self.client.logout()
            self.assertEqual(self.client.get(reverse("lancamento_comprovante", args=[l_pdf.pk])).status_code, 302)

    def test_importacao_csv_e_comandos_management(self):
        csv_bytes = (
            "Data;Descrição;Valor\n"
            "12/10/2026;Padaria Central;-25,50\n"
            "13/10/2026;Reembolso amigo;150,00\n"
        ).encode("utf-8")
        arq = SimpleUploadedFile("extrato.csv", csv_bytes, content_type="text/csv")
        itens = analisar_arquivo_extrato(arq, conta=self.conta)
        self.assertEqual(len(itens), 2)
        self.assertEqual((itens[0]["tipo"], itens[0]["valor"]), ("D", D("25.50")))
        self.assertEqual((itens[1]["tipo"], itens[1]["valor"]), ("R", D("150.00")))
        criados, ignorados = confirmar_importacao(itens, conta=self.conta, usuario=self.user)
        self.assertEqual((criados, ignorados), (2, 0))

        out = io.StringIO()
        call_command("recalcular_faturas", stdout=out)
        self.assertIn("Concluído", out.getvalue())

        out2 = io.StringIO()
        call_command("enviar_alertas", dry_run=True, stdout=out2)
        self.assertIn("Alertas encontrados", out2.getvalue())

    def test_comando_alertas_dry_run_e_telegram(self):
        Lancamento.objects.create(
            tipo="D", descricao="Conta vencendo", valor=D("120.00"),
            data=hoje(), vencimento=hoje() + timedelta(days=1), conta=self.conta, pago=False,
        )
        Integracao.objects.create(provedor=Integracao.TELEGRAM, ativo=True, segredo="tok", config={"chat_id": "123"})
        res_dry = enviar_alertas(dry_run=True)
        self.assertGreaterEqual(res_dry["total"], 1)
        self.assertIn("Conta vencendo", res_dry["mensagem"])

        class _FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch("financeiro.notificacoes.urllib.request.urlopen", return_value=_FakeResp()) as mock_url:
            res_send = enviar_alertas(dry_run=False)
            self.assertTrue(res_send["enviado"])
            self.assertTrue(mock_url.called)


class MoedaInputTests(Base):
    def test_normalizar_e_formatar_dinheiro_brl(self):
        self.assertEqual(normalizar_dinheiro_brl("1.234,56"), "1234.56")
        self.assertEqual(normalizar_dinheiro_brl("0,01"), "0.01")
        self.assertEqual(normalizar_dinheiro_brl("R$ 1.234,56"), "1234.56")
        self.assertEqual(normalizar_dinheiro_brl("-1.234,56"), "-1234.56")
        self.assertEqual(normalizar_dinheiro_brl("-R$ 1.234,56"), "-1234.56")
        self.assertEqual(normalizar_dinheiro_brl("R$ -1.234,56"), "-1234.56")
        self.assertEqual(normalizar_dinheiro_brl("250.50"), "250.50")
        self.assertEqual(normalizar_dinheiro_brl("0.01"), "0.01")
        self.assertEqual(normalizar_dinheiro_brl("3000"), "3000")
        self.assertEqual(normalizar_dinheiro_brl("0"), "0")
        self.assertEqual(normalizar_dinheiro_brl(""), "")
        self.assertIsNone(normalizar_dinheiro_brl(None))
        self.assertEqual(normalizar_dinheiro_brl("-R$ -10,00"), "-R$ -10,00")
        self.assertEqual(normalizar_dinheiro_brl("1,2,3"), "1,2,3")

        self.assertEqual(formatar_dinheiro_brl(D("1234.56")), "1.234,56")
        self.assertEqual(formatar_dinheiro_brl(D("-1234.56")), "-1.234,56")
        self.assertEqual(formatar_dinheiro_brl("R$ 50,00"), "50,00")
        self.assertEqual(formatar_dinheiro_brl(""), "")
        self.assertEqual(formatar_dinheiro_brl(None), "")

    def test_lancamento_form_moeda_brl(self):
        base = {
            "tipo": "D",
            "descricao": "Compra",
            "data": hoje().isoformat(),
            "forma": "conta",
            "conta": self.conta.pk,
            "categoria": self.mercado.pk,
            "pago": "on",
        }
        for valido, esperado in [
            ("1.234,56", D("1234.56")),
            ("R$ 250,50", D("250.50")),
            ("250.50", D("250.50")),
        ]:
            form = LancamentoForm(data={**base, "valor": valido})
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data["valor"], esperado)

        for invalido in ("abc", "1,2,3"):
            form = LancamentoForm(data={**base, "valor": invalido})
            self.assertFalse(form.is_valid())
            self.assertIn("valor", form.errors)

        lanc = Lancamento.objects.create(
            tipo="D", descricao="Edit", valor=D("1234.56"), data=hoje(), conta=self.conta
        )
        form_edit = LancamentoForm(instance=lanc)
        w = form_edit.fields["valor"].widget
        self.assertIsInstance(w, DinheiroInput)
        self.assertTrue(w.moeda_brl)
        self.assertEqual(w.attrs["data-moeda"], "brl")
        self.assertEqual(w.attrs["data-moeda-negativo"], "0")
        self.assertEqual(w.format_value(form_edit.initial["valor"]), "1.234,56")

    def test_pagamento_fatura_config_e_abastecimento(self):
        for v, esp in [("1.234,56", D("1234.56")), ("0.01", D("0.01"))]:
            f = PagamentoFaturaForm(data={"conta": self.conta.pk, "valor": v, "data": "2026-10-12"})
            self.assertTrue(f.is_valid(), f.errors)
            self.assertEqual(f.cleaned_data["valor"], esp)

        ContaForm = modelform_factory(
            Conta,
            form=ConfigForm,
            fields=["nome", "banco", "tipo", "titular", "saldo_inicial", "data_saldo_inicial", "ativo"],
        )
        fc = ContaForm(
            data={
                "nome": "Negativa",
                "tipo": "corrente",
                "titular": self.titular1.pk,
                "saldo_inicial": "-1.234,56",
                "data_saldo_inicial": "2026-01-01",
                "ativo": "on",
            }
        )
        self.assertEqual(fc.fields["saldo_inicial"].widget.attrs["data-moeda-negativo"], "1")
        self.assertTrue(fc.is_valid(), fc.errors)
        self.assertEqual(fc.cleaned_data["saldo_inicial"], D("-1234.56"))

        CartaoForm = modelform_factory(
            Cartao,
            form=ConfigForm,
            fields=["nome", "titular", "limite", "dia_fechamento", "dia_vencimento", "ativo"],
        )
        for lim_in, lim_out in [("", None), ("5.000,00", D("5000.00"))]:
            fcar = CartaoForm(
                data={
                    "nome": "Novo",
                    "titular": self.titular1.pk,
                    "limite": lim_in,
                    "dia_fechamento": "5",
                    "dia_vencimento": "12",
                    "ativo": "on",
                }
            )
            self.assertEqual(fcar.fields["limite"].widget.attrs["data-moeda-negativo"], "0")
            self.assertTrue(fcar.is_valid(), fcar.errors)
            self.assertEqual(fcar.cleaned_data["limite"], lim_out)

        fab = AbastecimentoForm()
        self.assertIsInstance(fab.fields["valor_total"].widget, DinheiroInput)
        self.assertTrue(fab.fields["valor_total"].widget.moeda_brl)
        self.assertNotIsInstance(fab.fields["litros"].widget, DinheiroInput)
        self.assertEqual(fab.fields["litros"].widget.attrs["step"], "0.001")

    def test_view_renderiza_prefixo_r_cifrao(self):
        self.user = User.objects.create_user(username="u_moeda", password="x")
        self.client.force_login(self.user)
        resp = self.client.get(reverse("lancamento_novo"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '<span class="input-group-text">R$</span>', html=False)
        self.assertContains(resp, 'data-moeda="brl"', html=False)


class AtualizacaoGitTests(Base):
    def setUp(self):
        super().setUp()
        self.comum = User.objects.create_user("usuario_comum", "comum@example.com", "SenhaForte#123")
        self.admin = User.objects.create_superuser("admin_git", "admin_git@example.com", "SenhaForte#123")
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.update_dir = Path(self.tmp_dir.name)

    def _escrever_host_info(self, **extras):
        from django.utils import timezone

        dados = {
            "commit": "1111111111111111111111111111111111111111",
            "commit_curto": "1111111",
            "branch": "main",
            "data_commit": "2026-11-24T10:00:00-03:00",
            "autor": "Dev",
            "mensagem": "Commit base",
            "repositorio": "joseluizpneto/financeiro-olpi",
            "alteracoes_locais": False,
            "heartbeat_em": timezone.now().isoformat(),
            **extras,
        }
        (self.update_dir / "host_info.json").write_text(json.dumps(dados), encoding="utf-8")

    def test_permissao_restrita_a_administradores(self):
        with override_settings(GIT_UPDATE_DIR=self.update_dir):
            self.client.force_login(self.comum)
            for url_name in ("admin_atualizacoes", "admin_atualizacao_status"):
                self.assertEqual(self.client.get(reverse(url_name)).status_code, 403)
            for url_name in (
                "admin_atualizacao_verificar",
                "admin_atualizacao_aplicar",
                "admin_atualizacao_cancelar",
            ):
                self.assertEqual(self.client.post(reverse(url_name)).status_code, 403)

            r_painel_comum = self.client.get(reverse("admin_painel"))
            self.assertEqual(r_painel_comum.status_code, 200)
            self.assertNotContains(r_painel_comum, reverse("admin_atualizacoes"))

            self.client.force_login(self.admin)
            r_adm = self.client.get(reverse("admin_atualizacoes"))
            self.assertEqual(r_adm.status_code, 200)
            self.assertContains(r_adm, "Atualizações via Git")

    def test_salvar_configuracao_normaliza_repo_e_mascara_token(self):
        with override_settings(GIT_UPDATE_DIR=self.update_dir):
            self.client.force_login(self.admin)
            r = self.client.post(
                reverse("admin_atualizacoes"),
                {
                    "repositorio": "git@github.com:joseluizpneto/financeiro-olpi.git",
                    "branch": "main",
                    "token": "github_pat_SECRETO_1234567890",
                },
            )
            self.assertEqual(r.status_code, 302)
            integ = Integracao.objects.get(provedor=Integracao.GIT)
            self.assertEqual(integ.config["repositorio"], "joseluizpneto/financeiro-olpi")
            self.assertEqual(integ.config["branch"], "main")
            self.assertEqual(integ.segredo, "github_pat_SECRETO_1234567890")

            r_get = self.client.get(reverse("admin_atualizacoes"))
            self.assertNotContains(r_get, "github_pat_SECRETO_1234567890")
            self.assertContains(r_get, "gith••••7890")
            self.assertContains(r_get, 'name="remover_token"', html=False)

            # Salvar com token em branco preserva o segredo anterior
            self.client.post(
                reverse("admin_atualizacoes"),
                {
                    "repositorio": "https://github.com/joseluizpneto/financeiro-olpi",
                    "branch": "main",
                    "token": "",
                },
            )
            integ.refresh_from_db()
            self.assertEqual(integ.segredo, "github_pat_SECRETO_1234567890")

            # Remover token salvo limpa o segredo e oculta o checkbox
            self.client.post(
                reverse("admin_atualizacoes"),
                {
                    "repositorio": "https://github.com/joseluizpneto/financeiro-olpi",
                    "branch": "main",
                    "token": "",
                    "remover_token": "on",
                },
            )
            integ.refresh_from_db()
            self.assertEqual(integ.segredo, "")
            r_sem_tok = self.client.get(reverse("admin_atualizacoes"))
            self.assertNotContains(r_sem_tok, "gith••••7890")
            self.assertNotContains(r_sem_tok, 'name="remover_token"', html=False)

    def test_verificar_atualizacoes_disponiveis_e_atualizado(self):
        self._escrever_host_info()
        Integracao.objects.create(
            provedor=Integracao.GIT,
            ativo=True,
            segredo="ghp_token_privado_abc123",
            config={"repositorio": "joseluizpneto/financeiro-olpi", "branch": "main"},
        )

        class _FakeResp:
            def __init__(self, payload: dict):
                self._raw = json.dumps(payload).encode("utf-8")

            def read(self):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        headers_capturados = {}

        def _fake_urlopen(req, timeout=10):
            headers_capturados.update({k.lower(): v for k, v in req.header_items()})
            return _FakeResp(
                {
                    "status": "ahead",
                    "ahead_by": 2,
                    "behind_by": 0,
                    "commits": [
                        {
                            "sha": "2222222222222222222222222222222222222222",
                            "html_url": "https://github.com/x/y/commit/2222222",
                            "commit": {"message": "feat: melhoria 1", "author": {"name": "Jose", "date": "2026-11-24T11:00:00Z"}},
                        },
                        {
                            "sha": "3333333333333333333333333333333333333333",
                            "html_url": "https://github.com/x/y/commit/3333333",
                            "commit": {"message": "fix: ajuste 2", "author": {"name": "Jose", "date": "2026-11-24T11:10:00Z"}},
                        },
                    ],
                    "files": [
                        {"filename": "financeiro/migrations/0005_teste.py"},
                        {"filename": "requirements.txt"},
                    ],
                }
            )

        with override_settings(GIT_UPDATE_DIR=self.update_dir), patch(
            "financeiro.atualizacao.urllib.request.urlopen", side_effect=_fake_urlopen
        ):
            self.client.force_login(self.admin)
            r = self.client.post(reverse("admin_atualizacao_verificar"))
            self.assertEqual(r.status_code, 302)
            self.assertEqual(headers_capturados.get("authorization"), "Bearer ghp_token_privado_abc123")

            r_page = self.client.get(reverse("admin_atualizacoes"))
            self.assertContains(r_page, "Atualização disponível")
            self.assertContains(r_page, "fix: ajuste 2")
            self.assertContains(r_page, "Inclui migrações de banco")
            self.assertContains(r_page, "Rebuild de imagem / dependências")
            self.assertContains(r_page, "Permitir e atualizar agora")

        self.assertTrue(
            RegistroAuditoria.objects.filter(modelo="sistema", acao=RegistroAuditoria.ADMIN).exists()
        )

        # Caso já atualizado
        with override_settings(GIT_UPDATE_DIR=self.update_dir), patch(
            "financeiro.atualizacao.urllib.request.urlopen",
            return_value=_FakeResp({"status": "identical", "ahead_by": 0, "behind_by": 0, "commits": [], "files": []}),
        ):
            self.client.post(reverse("admin_atualizacao_verificar"))
            r_ok = self.client.get(reverse("admin_atualizacoes"))
            self.assertContains(r_ok, "O sistema já está na versão mais recente.")
            self.assertNotContains(r_ok, "Permitir e atualizar agora")

    def test_verificar_erro_401_nao_vaza_token(self):
        self._escrever_host_info()
        Integracao.objects.create(
            provedor=Integracao.GIT,
            ativo=True,
            segredo="ghp_segredo_super_confidencial",
            config={"repositorio": "joseluizpneto/financeiro-olpi", "branch": "main"},
        )
        err = urllib.error.HTTPError("https://api.github.com/x", 401, "Unauthorized", {}, None)
        with override_settings(GIT_UPDATE_DIR=self.update_dir), patch(
            "financeiro.atualizacao.urllib.request.urlopen", side_effect=err
        ):
            self.client.force_login(self.admin)
            self.client.post(reverse("admin_atualizacao_verificar"))
            r = self.client.get(reverse("admin_atualizacoes"))
            self.assertContains(r, "HTTP 401")
            self.assertNotContains(r, "ghp_segredo_super_confidencial")

    def test_aplicar_exige_confirmacao_cria_pedido_bloqueia_concorrencia_e_cancela(self):
        self._escrever_host_info()
        Integracao.objects.create(
            provedor=Integracao.GIT,
            ativo=True,
            config={
                "repositorio": "joseluizpneto/financeiro-olpi",
                "branch": "main",
                "ultima_verificacao": {
                    "ok": True,
                    "estado": "disponivel",
                    "tem_atualizacao": True,
                    "commit_local": "1111111111111111111111111111111111111111",
                    "commit_remoto": "3333333333333333333333333333333333333333",
                    "commit_remoto_curto": "3333333",
                    "mensagem_status": "Há 1 alteração disponível.",
                },
            },
        )
        with override_settings(GIT_UPDATE_DIR=self.update_dir):
            self.client.force_login(self.admin)

            # Sem checkbox de confirmação não cria pedido
            self.client.post(reverse("admin_atualizacao_aplicar"), {})
            self.assertFalse((self.update_dir / "pedido.json").exists())

            # Com confirmação cria pedido.json
            self.client.post(reverse("admin_atualizacao_aplicar"), {"confirmar": "1"})
            pedido_path = self.update_dir / "pedido.json"
            self.assertTrue(pedido_path.exists())
            pedido = json.loads(pedido_path.read_text(encoding="utf-8"))
            self.assertEqual(pedido["branch"], "main")
            self.assertEqual(pedido["commit_alvo"], "3333333333333333333333333333333333333333")
            self.assertEqual(pedido["solicitado_por"], "admin_git")

            # Endpoint JSON de status
            r_status = self.client.get(reverse("admin_atualizacao_status"))
            self.assertEqual(r_status.status_code, 200)
            payload = json.loads(r_status.content)
            self.assertEqual(payload["execucao"]["estado"], "pendente")

            # Segunda solicitação simultânea é bloqueada
            self.client.post(reverse("admin_atualizacao_aplicar"), {"confirmar": "1"})
            pedido_depois = json.loads(pedido_path.read_text(encoding="utf-8"))
            self.assertEqual(pedido["id"], pedido_depois["id"])

            # Cancelar remove pedido.json
            self.client.post(reverse("admin_atualizacao_cancelar"))
            self.assertFalse(pedido_path.exists())

            # Alterações locais no servidor bloqueiam solicitação
            self._escrever_host_info(alteracoes_locais=True)
            self.client.post(reverse("admin_atualizacao_aplicar"), {"confirmar": "1"})
            self.assertFalse(pedido_path.exists())
