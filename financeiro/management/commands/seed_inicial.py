"""Cria os cadastros iniciais (só quando ainda não existem)."""
import os

from django.core.management.base import BaseCommand
from django.db import transaction

from financeiro.models import Categoria, CentroCusto, Titular

TITULARES_PADRAO = [("Titular 1", "PF"), ("Titular 2", "PF")]


def _nome_empresa_seed() -> str:
    empresa_nome = os.environ.get("SEED_EMPRESA_NOME", "").strip()[:60]
    if not empresa_nome or empresa_nome.lower() in ("casa", "veículo", "veiculo"):
        return ""
    return empresa_nome


def _obter_titulares_iniciais() -> list[tuple[str, str]]:
    bruto = os.environ.get("SEED_TITULARES", "").strip()
    if not bruto:
        titulares = list(TITULARES_PADRAO)
        empresa_nome = _nome_empresa_seed()
        if empresa_nome:
            titulares.append((empresa_nome, "PJ"))
        return titulares
    resultado: list[tuple[str, str]] = []
    vistos: set[str] = set()
    for item in bruto.split(","):
        parte = item.strip()
        if not parte:
            continue
        if ":" in parte:
            nome_raw, tipo_raw = parte.rsplit(":", 1)
            tipo = "PJ" if tipo_raw.strip().upper() == "PJ" else "PF"
        else:
            nome_raw, tipo = parte, "PF"
        nome = nome_raw.strip()[:100]
        if not nome:
            continue
        chave = nome.lower()
        if chave in vistos:
            continue
        vistos.add(chave)
        resultado.append((nome, tipo))
    if resultado:
        return resultado
    titulares = list(TITULARES_PADRAO)
    empresa_nome = _nome_empresa_seed()
    if empresa_nome:
        titulares.append((empresa_nome, "PJ"))
    return titulares


def _obter_centros_iniciais() -> list[tuple[str, str, bool, str]]:
    centros = [
        ("Casa", "Despesas e receitas residenciais e pessoais", False, "casa"),
        ("Veículo", "Combustível, manutenção e custos do carro", False, "veiculo"),
    ]
    empresa_nome = _nome_empresa_seed()
    if empresa_nome:
        empresa_desc = (
            os.environ.get("SEED_EMPRESA_DESCRICAO", "").strip()[:200]
            or "Receitas e despesas da empresa"
        )
        centros.append((empresa_nome, empresa_desc, True, "empresa"))
    return centros

DESPESAS = {
    "Moradia": ["Aluguel / financiamento", "Condomínio", "Energia", "Água", "Internet e telefone", "Gás", "Manutenção da casa", "Móveis e utensílios"],
    "Alimentação": ["Supermercado", "Restaurantes e delivery", "Padaria e feira"],
    "Transporte": ["Aplicativo / táxi", "Transporte público", "Estacionamento e pedágio"],
    "Veículo": ["Combustível", "Manutenção", "IPVA", "Seguro", "Licenciamento", "Multas", "Lavagem e outros"],
    "Saúde": ["Plano de saúde", "Farmácia", "Consultas e exames"],
    "Educação": ["Cursos", "Material escolar", "Livros"],
    "Lazer": ["Viagens", "Passeios", "Assinaturas e streaming"],
    "Pessoal": ["Vestuário", "Beleza e cuidados", "Presentes"],
    "Pets": [],
    "Despesas financeiras": ["Tarifas bancárias", "Juros e multas", "Anuidade de cartão"],
    "Empresa": ["Fornecedores", "Materiais e insumos", "Serviços de terceiros", "Contabilidade", "Software e assinaturas", "Marketing", "Equipamentos"],
    "Impostos": ["DAS / Simples Nacional", "ISS", "INSS", "Imposto de renda", "Taxas e outros tributos"],
    "Outras despesas": [],
}

RECEITAS = {
    "Salário": [],
    "Serviços prestados": [],
    "Vendas": [],
    "Pró-labore e distribuição de lucros": [],
    "Rendimentos": [],
    "Reembolsos e estornos": [],
    "Outras receitas": [],
}

CODIGOS_CATEGORIAS = {
    ("Veículo", None): "veiculo",
    ("Combustível", "Veículo"): "combustivel",
    ("Manutenção", "Veículo"): "manutencao",
}


class Command(BaseCommand):
    help = "Cria titulares, centros de custo e categorias padrão, se ainda não existirem."

    @transaction.atomic
    def handle(self, *args, **opts):
        if not Titular.objects.exists():
            for nome, tipo in _obter_titulares_iniciais():
                Titular.objects.create(nome=nome, tipo=tipo)
            self.stdout.write("Titulares criados.")
        centros = _obter_centros_iniciais()
        if not CentroCusto.objects.exists():
            for nome, descricao, empresa, codigo in centros:
                CentroCusto.objects.create(nome=nome, descricao=descricao, empresa=empresa, codigo_sistema=codigo)
            self.stdout.write("Centros de custo criados.")
        else:
            for nome, _, _, codigo in centros:
                if not CentroCusto.objects.filter(codigo_sistema=codigo).exists():
                    CentroCusto.objects.filter(nome__iexact=nome, codigo_sistema__isnull=True).update(codigo_sistema=codigo)

        if not Categoria.objects.exists():
            for tipo, arvore in (("D", DESPESAS), ("R", RECEITAS)):
                for principal, filhas in arvore.items():
                    pai = Categoria.objects.create(
                        nome=principal,
                        tipo=tipo,
                        imposto=(principal == "Impostos"),
                        codigo_sistema=CODIGOS_CATEGORIAS.get((principal, None)),
                    )
                    for filha in filhas:
                        Categoria.objects.create(
                            nome=filha,
                            tipo=tipo,
                            pai=pai,
                            codigo_sistema=CODIGOS_CATEGORIAS.get((filha, principal)),
                        )
            self.stdout.write("Categorias padrão criadas.")
        else:
            for (nome, pai_nome), codigo in CODIGOS_CATEGORIAS.items():
                if not Categoria.objects.filter(codigo_sistema=codigo).exists():
                    qs = Categoria.objects.filter(tipo="D", nome__iexact=nome, codigo_sistema__isnull=True)
                    qs = qs.filter(pai__nome__iexact=pai_nome) if pai_nome else qs.filter(pai__isnull=True)
                    obj = qs.first()
                    if obj:
                        obj.codigo_sistema = codigo
                        obj.save(update_fields=["codigo_sistema"])
        self.stdout.write(self.style.SUCCESS("Cadastros iniciais conferidos."))
