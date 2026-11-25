from django.db import migrations, models


def preencher_formas_pagamento(apps, schema_editor):
    Lancamento = apps.get_model("financeiro", "Lancamento")
    Recorrencia = apps.get_model("financeiro", "Recorrencia")

    Lancamento.objects.filter(tipo__in=["R", "D", "E"], cartao__isnull=False).update(forma_pagamento="credito")
    Lancamento.objects.filter(tipo__in=["R", "D", "E"], conta__tipo="carteira").update(forma_pagamento="dinheiro")
    Lancamento.objects.filter(
        tipo__in=["R", "D", "E"], conta__isnull=False, forma_pagamento=""
    ).update(forma_pagamento="pix")

    Recorrencia.objects.filter(cartao__isnull=False).update(forma_pagamento="credito")
    Recorrencia.objects.filter(conta__tipo="carteira").update(forma_pagamento="dinheiro")
    Recorrencia.objects.filter(conta__isnull=False, forma_pagamento="").update(forma_pagamento="pix")


class Migration(migrations.Migration):

    dependencies = [
        ("financeiro", "0007_centrocusto_escopo"),
    ]

    operations = [
        migrations.AddField(
            model_name="lancamento",
            name="forma_pagamento",
            field=models.CharField(
                blank=True,
                choices=[("pix", "PIX"), ("debito", "Débito"), ("credito", "Crédito"), ("dinheiro", "Dinheiro")],
                default="",
                max_length=12,
                verbose_name="forma de pagamento",
            ),
        ),
        migrations.AddField(
            model_name="recorrencia",
            name="forma_pagamento",
            field=models.CharField(
                blank=True,
                choices=[("pix", "PIX"), ("debito", "Débito"), ("credito", "Crédito"), ("dinheiro", "Dinheiro")],
                default="",
                max_length=12,
                verbose_name="forma de pagamento",
            ),
        ),
        migrations.RunPython(preencher_formas_pagamento, migrations.RunPython.noop),
    ]
