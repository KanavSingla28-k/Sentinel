"""Policy resolution for Sentinel (Phase 5).

The resolver maps an incoming (tenant_id, endpoint_id) pair to its static
Policy. Policies are indexed only by endpoint_id; the tenant dimension exists
so the resolver can reject requests without a usable tenant claim without
raising. Tenant hashing (Phase 6) and JWT validation (Phase 7) live elsewhere.
"""

from typing import Protocol

from sentinel.config import SentinelConfig
from sentinel.models import Policy


class PolicyResolver(Protocol):
    def resolve(
        self,
        tenant_id: str | None,
        endpoint_id: str,
    ) -> Policy | None:
        """Return the policy for a tenant and endpoint, or None if unknown.

        Missing tenant claims (None or empty) and unknown endpoints yield None;
        the resolver never raises for these normal missing-input cases.
        """
        ...


class StaticPolicyResolver:
    def __init__(self, config: SentinelConfig) -> None:
        self._policies = config.policies

    def resolve(
        self,
        tenant_id: str | None,
        endpoint_id: str,
    ) -> Policy | None:
        if not tenant_id:
            return None
        return self._policies.get(endpoint_id)
