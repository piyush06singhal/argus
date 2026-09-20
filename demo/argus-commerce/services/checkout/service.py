"""Checkout service — the failing call path (ARGUS demo).

``process()`` retries ``InventoryRepository.fetch_stock`` with linear backoff.
That loop is only dangerous because the repository's query timeout was
configured below one second: every attempt fails immediately and the retries
multiply a fast failure into a multi-second request.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from services.checkout.retry import RETRY_ATTEMPTS, backoff_seconds
from services.inventory.repository import InventoryRepository


class CheckoutService:
    """Coordinates stock reservation for a checkout request."""

    def __init__(self, repository: InventoryRepository) -> None:
        self.repository = repository

    async def process(self, sku: str, quantity: int) -> Dict[str, Any]:
        """Reserve stock, retrying the inventory lookup on transient timeouts."""
        last_error: Optional[BaseException] = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return await self.reserve(sku, quantity)
            except TimeoutError as error:
                last_error = error
                await asyncio.sleep(backoff_seconds(attempt))
        raise last_error or RuntimeError("checkout failed without an error")

    async def reserve(self, sku: str, quantity: int) -> Dict[str, Any]:
        """Validate the SKU and reserve the requested quantity."""
        stock = await self.repository.fetch_stock(sku)
        if not stock:
            raise ValueError(f"unknown sku: {sku}")
        return {"sku": sku, "quantity": quantity, "reserved": True}
