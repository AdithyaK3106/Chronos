"""cng_task_02 acceptance: timezone-naive expiry dates from a provider.

A provider may return `expires_at` without tzinfo. It must be treated as UTC and classified
correctly instead of raising TypeError when compared with the timezone-aware current time.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.models import ComplianceStatus, ProviderRecord
from app.ports.compliance_provider import ComplianceProvider
from app.services.verification_service import VerificationService


class _FixedProvider(ComplianceProvider):
    def __init__(self, expires_at: datetime) -> None:
        self._expires_at = expires_at

    async def lookup(self, vehicle_registration: str) -> ProviderRecord | None:
        return ProviderRecord(
            vehicle_registration=vehicle_registration,
            compliance_id="acc-1",
            is_valid=True,
            expires_at=self._expires_at,
        )


def _naive_utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.mark.parametrize(
    "offset, expected",
    [
        (timedelta(days=200), ComplianceStatus.VALID),
        (timedelta(days=10), ComplianceStatus.EXPIRING_SOON),
        (timedelta(days=-5), ComplianceStatus.EXPIRED),
    ],
)
def test_naive_expiry_is_classified(offset: timedelta, expected: ComplianceStatus) -> None:
    service = VerificationService(_FixedProvider(_naive_utc_now() + offset))
    result = asyncio.run(service.verify("DL01AB1234"))
    assert result.status == expected


def test_aware_expiry_still_works() -> None:
    service = VerificationService(_FixedProvider(datetime.now(UTC) + timedelta(days=200)))
    result = asyncio.run(service.verify("DL01AB1234"))
    assert result.status == ComplianceStatus.VALID
