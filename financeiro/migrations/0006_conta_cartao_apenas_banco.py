from django.db import migrations, models


def copiar_nome_para_banco(apps, schema_editor):
    for modelo_nome in ("Conta", "Cartao"):
        Modelo = apps.get_model("financeiro", modelo_nome)
        for obj in Modelo.objects.all():
            if not (obj.banco or "").strip():
                obj.banco = obj.nome
                obj.save(update_fields=["banco"])


def copiar_banco_para_nome(apps, schema_editor):
    for modelo_nome in ("Conta", "Cartao"):
        Modelo = apps.get_model("financeiro", modelo_nome)
        for obj in Modelo.objects.all():
            if not (obj.nome or "").strip():
                obj.nome = obj.banco
                obj.save(update_fields=["nome"])


class Migration(migrations.Migration):

    dependencies = [
        ("financeiro", "0005_configuracaosistema"),
    ]

    operations = [
        migrations.RunPython(copiar_nome_para_banco, copiar_banco_para_nome),
        migrations.AlterField(
            model_name="conta",
            name="banco",
            field=models.CharField(max_length=80, verbose_name="banco"),
        ),
        migrations.AlterField(
            model_name="cartao",
            name="banco",
            field=models.CharField(max_length=80, verbose_name="banco"),
        ),
        migrations.RemoveField(
            model_name="conta",
            name="nome",
        ),
        migrations.RemoveField(
            model_name="cartao",
            name="nome",
        ),
        migrations.AlterModelOptions(
            name="conta",
            options={
                "ordering": ["titular__nome", "banco"],
                "verbose_name": "conta bancária",
                "verbose_name_plural": "contas bancárias",
            },
        ),
        migrations.AlterModelOptions(
            name="cartao",
            options={
                "ordering": ["banco"],
                "verbose_name": "cartão de crédito",
                "verbose_name_plural": "cartões de crédito",
            },
        ),
    ]
