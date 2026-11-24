from django.core.management.base import BaseCommand

from financeiro.notificacoes import enviar_alertas


class Command(BaseCommand):
    help = "Verifica contas a vencer, faturas, orçamento e veículos e envia alertas configurados."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Mostra a mensagem sem enviar.")

    def handle(self, *args, **opts):
        res = enviar_alertas(dry_run=opts["dry_run"])
        self.stdout.write(f"Alertas encontrados: {res['total']} | Status: {res['motivo']}")
        if opts["dry_run"]:
            self.stdout.write(res["mensagem"])
