from django.core.management.base import BaseCommand

from financeiro.services import gerar_recorrencias


class Command(BaseCommand):
    help = "Gera os lançamentos das recorrências para os próximos 45 dias."

    def handle(self, *args, **opts):
        total = gerar_recorrencias()
        self.stdout.write(self.style.SUCCESS(f"{total} lançamento(s) gerado(s)."))
