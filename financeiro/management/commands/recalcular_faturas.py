from django.core.management.base import BaseCommand

from financeiro.services import recalcular_todas_faturas


class Command(BaseCommand):
    help = "Recalcula o status pago/aberto de todas as faturas de cartão."

    def handle(self, *args, **opts):
        alteradas = recalcular_todas_faturas()
        self.stdout.write(self.style.SUCCESS(f"Concluído. Faturas atualizadas: {alteradas}."))
