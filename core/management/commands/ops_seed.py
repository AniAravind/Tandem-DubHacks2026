"""Create an isolated, repeatable fixture; never call the shared demo reset."""
import hashlib
import re
import uuid
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from django.db import transaction
from core.models import Group,Member
from merchant.models import Merchant,Listing


class Command(BaseCommand):
    help='Create or reuse a namespaced simulated fixture without resetting existing groups or inventory.'
    def add_arguments(self,parser):
        parser.add_argument('--namespace',required=True)
        parser.add_argument('--allow-deployed-isolated-fixture',action='store_true')
    @transaction.atomic
    def handle(self,*args,**options):
        if not settings.DEMO_MODE:
            raise CommandError('Isolated fixtures require DEMO_MODE=1.')
        namespace=options['namespace']
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}',namespace):
            raise CommandError('Namespace must be 1–32 lowercase letters, numbers, or hyphens, starting with a letter.')
        if settings.TANDEM_DEPLOYMENT and not options['allow_deployed_isolated_fixture']:
            raise CommandError('Deployed fixtures require --allow-deployed-isolated-fixture. No data was changed.')
        identity=uuid.uuid5(uuid.NAMESPACE_URL,'tandem:ops-fixture:'+namespace)
        name='OPS fixture: '+namespace
        group,created=Group.objects.get_or_create(pk=identity,defaults={'name':name,'channel':'web'})
        if group.name!=name:
            raise CommandError('Fixture identity collision; no existing group will be overwritten.')
        for index,display in enumerate(('Demo organizer','Demo buyer 2','Demo buyer 3')):
            member_id=uuid.uuid5(identity,'member:'+str(index))
            member,_=Member.objects.get_or_create(pk=member_id,defaults={'group':group,'display_name':display,
                'role':'organizer' if index==0 else 'member','phone_e164_hash':hashlib.sha256((str(identity)+':'+str(index)).encode()).hexdigest()})
            if member.group_id!=group.id:
                raise CommandError('Fixture member collision; no member will be moved.')
            if created and index==0:
                group.organizer=member;group.save(update_fields=['organizer'])
        slug='ops-fixture-'+identity.hex[:12]
        merchant,_=Merchant.objects.get_or_create(slug=slug,defaults={'name':name,'category':'gift_bundle','mcc':'5947'})
        if merchant.name!=name:
            raise CommandError('Fixture merchant collision; inventory will not be modified.')
        listing,_=Listing.objects.get_or_create(merchant=merchant,ref='shared-gift',defaults={'title':'Simulated shared gift',
            'total_cents':2400,'capacity':8,'features':['gift_wrap'],'meta':{'demo_data':True,'ops_fixture':namespace}})
        if listing.meta.get('ops_fixture')!=namespace:
            raise CommandError('Fixture listing collision; inventory will not be modified.')
        self.stdout.write(f'Isolated fixture ready: {group.id}. No purchases, holds, or shared demo reset were created.')
