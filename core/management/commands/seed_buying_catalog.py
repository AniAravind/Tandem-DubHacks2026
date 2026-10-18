from django.core.management.base import BaseCommand
from django.db import transaction
from merchant.models import Merchant, Listing


class Command(BaseCommand):
    help = 'Add explicitly simulated gift offers without resetting any users, groups, or purchases.'

    @transaction.atomic
    def handle(self, *args, **kwargs):
        merchant, _ = Merchant.objects.get_or_create(slug='giftstudio', defaults={'name':'Gift Studio (demo)','category':'gift_bundle','mcc':'5947'})
        for ref, title, cents, features in [
            ('game-night','Game night gift set',2499,['board_games','gift_wrap']),
            ('coffee-tasting','Coffee tasting gift box',6000,['coffee','gift_wrap']),
            ('creative-weekend','Creative weekend gift kit',9900,['art','gift_wrap']),
        ]:
            Listing.objects.get_or_create(merchant=merchant, ref=ref, defaults={'title':title,'total_cents':cents,'capacity':20,
                'features':features,'meta':{'demo_data':True,'cancellation':'Simulated gift order; no real fulfillment.'}})
        self.stdout.write('Simulated gift catalog is available; existing group data was preserved.')
