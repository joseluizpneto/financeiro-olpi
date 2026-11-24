from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("financeiro", "0003_codigos_sistema"),
    ]

    operations = [
        migrations.AlterField(
            model_name="integracao",
            name="provedor",
            field=models.CharField(
                choices=[
                    ("telegram", "Bot do Telegram"),
                    ("ia", "Assistente de IA (LLM)"),
                    ("alertas", "Alertas automáticos"),
                    ("email", "Notificações por e-mail"),
                    ("git", "Atualização via Git"),
                ],
                max_length=20,
                unique=True,
                verbose_name="provedor",
            ),
        ),
    ]
