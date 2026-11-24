from django.db import migrations


def popular_codigos_sistema(apps, schema_editor):
    CentroCusto = apps.get_model("financeiro", "CentroCusto")
    Categoria = apps.get_model("financeiro", "Categoria")

    centros = {
        "Casa": "casa",
        "Veículo": "veiculo",
        "Empresa": "empresa",
    }
    for nome, codigo in centros.items():
        if not CentroCusto.objects.filter(codigo_sistema=codigo).exists():
            CentroCusto.objects.filter(nome__iexact=nome, codigo_sistema__isnull=True).update(
                codigo_sistema=codigo
            )

    pai = (
        Categoria.objects.filter(tipo="D", codigo_sistema="veiculo", pai__isnull=True).first()
        or Categoria.objects.filter(tipo="D", nome__iexact="Veículo", pai__isnull=True).first()
    )
    if pai and not pai.codigo_sistema:
        pai.codigo_sistema = "veiculo"
        pai.save(update_fields=["codigo_sistema"])

    filhas = {"Combustível": "combustivel", "Manutenção": "manutencao"}
    for nome, codigo in filhas.items():
        if Categoria.objects.filter(codigo_sistema=codigo).exists():
            continue
        qs = Categoria.objects.filter(tipo="D", nome__iexact=nome, codigo_sistema__isnull=True)
        if pai:
            qs = qs.filter(pai=pai)
        obj = qs.first()
        if obj:
            obj.codigo_sistema = codigo
            obj.save(update_fields=["codigo_sistema"])


class Migration(migrations.Migration):

    dependencies = [
        ("financeiro", "0002_fechamentomensal_integracao_metafinanceira_and_more"),
    ]

    operations = [
        migrations.RunPython(popular_codigos_sistema, reverse_code=migrations.RunPython.noop),
    ]
