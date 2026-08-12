"""RGNR8 delivery runtime — a durable, long-running weekly-briefing sender."""

from __future__ import annotations

from .manager import SubscriptionManager
from .runtime import DeliveryRuntime
from .serve import serve
from .subscriptions import (
    InMemorySubscriptionStore,
    SqlSubscriptionStore,
    SubscriptionStore,
)
from .tenants import InMemoryTenantSource, RuntimeTenant, TenantSource

__version__ = "0.1.0"

__all__ = [
    "DeliveryRuntime",
    "serve",
    "SubscriptionStore",
    "InMemorySubscriptionStore",
    "SqlSubscriptionStore",
    "SubscriptionManager",
    "TenantSource",
    "InMemoryTenantSource",
    "RuntimeTenant",
]
