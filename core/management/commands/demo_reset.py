from django.core.management.base import BaseCommand

from purchases import demo


class Command(BaseCommand):
    help = "Wipe the demo group's activity (purchases, votes, chat, events) and load the seed data again. DEMO ONLY."

    def handle(self, *args, **opts):
        result = demo.reset_seed()
        self.stdout.write(self.style.SUCCESS(f"Demo reset: group {result['group_id']} with {result['members']} members"))
