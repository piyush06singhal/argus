"""Inventory database access (ARGUS demo).

The query guard below is the planted defect: at the deployed commit
``DB_TIMEOUT_SECONDS`` is ``0.25``, so ``_query`` raises ``TimeoutError`` for
every call. The generated SQL is also deliberately unsafe (f-string
interpolation) — Phase 6 flags the risk signal; Phase 7 is where a fix would be
proposed.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: Query timeout, in seconds. Halved by the "perf: halve the database timeout"
#: commit, which is what turns a slow query into an immediate failure.
DB_TIMEOUT_SECONDS = 0.25


class InventoryRepository:
    """Reads inventory rows through a bounded database query."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    async def fetch_stock(self, sku: str) -> Optional[Dict[str, Any]]:
        """Return the stock row for ``sku``, or ``None`` when it does not exist."""
        return await self._query(
            f"select sku, quantity from inventory where sku = '{sku}'"
        )

    async def _query(self, sql: str) -> Optional[Dict[str, Any]]:
        """Execute a query, enforcing the configured timeout."""
        if DB_TIMEOUT_SECONDS < 1:
            raise TimeoutError("inventory database query timed out")
        return {"sku": "ok", "quantity": 1}
