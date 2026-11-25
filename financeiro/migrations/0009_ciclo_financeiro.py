from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("financeiro", "0008_forma_pagamento"),
    ]

    operations = [
        migrations.AddField(
            model_name="configuracaosistema",
            name="dia_inicio_ciclo",
            field=models.PositiveSmallIntegerField(
                default=1,
                help_text="Use 1 para mês calendário (01 a 31). Ex.: 25 para iniciar no dia em que o salário cai.",
                validators=[MinValueValidator(1), MaxValueValidator(31)],
                verbose_name="dia de início do ciclo mensal",
            ),
        ),
        migrations.AddField(
            model_name="configuracaosistema",
            name="referencia_ciclo",
            field=models.CharField(
                choices=[
                    ("seguinte", "Mês seguinte (ex.: 25/10 a 24/11 = Novembro)"),
                    ("atual", "Mês de início (ex.: 05/11 a 04/12 = Novembro)"),
                ],
                default="seguinte",
                help_text="Quando o ciclo não começa no dia 1, define qual mês dá nome ao período.",
                max_length=10,
                verbose_name="mês de referência do ciclo",
            ),
        ),
        migrations.AddConstraint(
            model_name="configuracaosistema",
            constraint=models.CheckConstraint(
                condition=models.Q(("dia_inicio_ciclo__gte", 1), ("dia_inicio_ciclo__lte", 31)),
                name="config_dia_inicio_ciclo_valido",
            ),
        ),
    ]
