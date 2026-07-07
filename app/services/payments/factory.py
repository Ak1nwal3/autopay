"""Build a Nomba provider for FastAPI dependency injection.

In the parent auto-pay-ai project the equivalent helper chooses
between Paystack and Nomba. This project is Nomba-only, so the
factory is a one-liner; the Protocol-shaped seam is preserved so
the topup / wallet code is unchanged.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.services.payments.nomba import NombaProvider
from app.services.payments.base import PaymentProvider


def get_payment_provider() -> PaymentProvider:
    """Return a Nomba provider configured from settings.

    Tests can override this with FastAPI's `app.dependency_overrides`.
    """
    s = get_settings()
    return NombaProvider(
        base_url=s.nomba_base_url,
        client_id=s.nomba_client_id,
        client_secret=s.nomba_client_secret,
        account_id=s.nomba_account_id,
        webhook_secret=s.nomba_webhook_secret,
        is_sandbox=s.nomba_sandbox,
    )
