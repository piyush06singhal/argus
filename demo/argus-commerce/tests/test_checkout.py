"""The test that would have caught the regression (ARGUS demo)."""

from __future__ import annotations

import pytest

from services.checkout.service import CheckoutService
from services.inventory.repository import InventoryRepository


@pytest.mark.asyncio
async def test_checkout_reserves_stock_within_one_attempt():
    """A healthy database reserves stock without exhausting the retry budget."""
    service = CheckoutService(InventoryRepository(dsn="postgresql://inventory"))
    result = await service.reserve("sku-1", 1)
    assert result == {"sku": "sku-1", "quantity": 1, "reserved": True}
