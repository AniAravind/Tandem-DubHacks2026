"""Check Visa Acceptance sandbox credentials: one $1.00 test authorization, immediately reversed.

    python manage.py visa_sandbox_check

Needs VISA_MERCHANT_ID, VISA_KEY_ID, VISA_SHARED_SECRET (sandbox). Uses Visa's test card; no real money.
"""
import uuid

from django.core.management.base import BaseCommand, CommandError

from payments.gateway import GatewayError
from payments.visa_acceptance import TEST_BILL_TO, TEST_CARD, VisaClient, configured


class Command(BaseCommand):
    help = "Make one $1.00 Visa sandbox authorization and reverse it."

    def handle(self, *args, **opts):
        if not configured():
            raise CommandError("Set VISA_MERCHANT_ID, VISA_KEY_ID and VISA_SHARED_SECRET first (sandbox keys).")
        client = VisaClient.from_env()
        ref = "tandem-check-" + uuid.uuid4().hex[:8]
        try:
            auth = client.post("/pts/v2/payments", {
                "clientReferenceInformation": {"code": ref}, "processingInformation": {"capture": False},
                "paymentInformation": {"card": TEST_CARD},
                "orderInformation": {"amountDetails": {"totalAmount": "1.00", "currency": "USD"}, "billTo": TEST_BILL_TO}})
            self.stdout.write(f"Authorization: {auth.get('status')} id={auth.get('id')}")
            if auth.get("id"):
                rev = client.post(f"/pts/v2/payments/{auth['id']}/reversals", {
                    "clientReferenceInformation": {"code": ref},
                    "reversalInformation": {"amountDetails": {"totalAmount": "1.00"}, "reason": "credential check"}})
                self.stdout.write(f"Reversal: {rev.get('status')}")
        except GatewayError as e:
            raise CommandError(f"Visa sandbox rejected the request: {e.code} ({e}). Check the three keys and that they are SANDBOX keys.")
        if auth.get("status") != "AUTHORIZED":
            raise CommandError("Visa answered but did not authorize. See the status above.")
        self.stdout.write(self.style.SUCCESS("Visa sandbox credentials work."))
