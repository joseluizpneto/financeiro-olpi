from django.db import migrations, models


def migrar_empresa_para_escopo(apps, schema_editor):
    CentroCusto = apps.get_model("financeiro", "CentroCusto")
    CentroCusto.objects.filter(empresa=True).update(escopo="PJ")


def reverter_escopo_para_empresa(apps, schema_editor):
    CentroCusto = apps.get_model("financeiro", "CentroCusto")
    CentroCusto.objects.filter(escopo="PJ").update(empresa=True)


class Migration(migrations.Migration):

    dependencies = [
        ("financeiro", "0006_conta_cartao_apenas_banco"),
    ]

    operations = [
        migrations.AddField(
            model_name="centrocusto",
            name="escopo",
            field=models.CharField(
                choices=[("PF", "Pessoal"), ("PJ", "Empresarial")],
                default="PF",
                help_text="Centros empresariais aparecem no relatório da empresa e no filtro Empresa (PJ).",
                max_length=2,
                verbose_name="escopo",
            ),
        ),
        migrations.RunPython(migrar_empresa_para_escopo, reverter_escopo_para_empresa),
        migrations.RemoveField(
            model_name="centrocusto",
            name="empresa",
        ),
    ]
