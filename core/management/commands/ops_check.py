import json
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from django.test.utils import override_settings
from core.checks import deployment_problems
from tandem.health import dependency_status


class Command(BaseCommand):
    help='Validate deployment configuration without showing values. Optional read-only dependency probes.'
    def add_arguments(self,parser):
        parser.add_argument('--deployment',action='store_true')
        parser.add_argument('--dependencies',action='store_true')
    def handle(self,*args,**options):
        with override_settings(TANDEM_DEPLOYMENT=settings.TANDEM_DEPLOYMENT or options['deployment']):
            problems=deployment_problems()
            result={'configuration':'error' if problems else 'ok',
                'errors':[{'id':error.id,'message':error.msg} for error in problems]}
            if options['dependencies']:
                result['dependencies']=dependency_status()
            self.stdout.write(json.dumps(result))
            if problems or any(value not in ('ok','skipped') for value in result.get('dependencies',{}).values()):
                raise CommandError('Operational checks failed; diagnostics above contain setting names only.')
