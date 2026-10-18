"""Which payment rail is active, as an honest label. Lives outside payments/ so agent/ and chat/ may read it
without importing the gateway (they must never touch money code)."""
from django.conf import settings


def payments_mode() -> str:
    """'visa_sandbox' when holds really go to Visa's sandbox, else 'simulated'."""
    return "visa_sandbox" if settings.PAYMENTS_GATEWAY == "visa_sandbox" else "simulated"


def payments_label() -> str:
    return "Visa sandbox · test mode" if payments_mode() == "visa_sandbox" else "simulated"
