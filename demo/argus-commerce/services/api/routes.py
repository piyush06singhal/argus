"""HTTP surface for the checkout flow (ARGUS demo).

Route declarations are what let the trace-to-code mapper turn an observed span
(``POST /api/checkout``) into the handler and the service call it reaches. The
paths below deliberately match the operations the demo project emits, so the
mapping is discoverable from stored evidence rather than assumed.
"""

from __future__ import annotations

from typing import Any, Dict


class Router:
    """A minimal stand-in for a framework router.

    Only the decorator form is used, because that is what the parser resolves to
    a ``(route, handler)`` pair without executing anything.
    """

    def __init__(self) -> None:
        self.registered: Dict[str, str] = {}

    def post(self, path: str):
        def decorator(handler):
            self.registered[f"POST {path}"] = handler.__name__
            return handler

        return decorator

    def get(self, path: str):
        def decorator(handler):
            self.registered[f"GET {path}"] = handler.__name__
            return handler

        return decorator


router = Router()


def _service():
    """Resolve the checkout service for a request."""
    from services.checkout.service import CheckoutService
    from services.inventory.repository import InventoryRepository

    return CheckoutService(InventoryRepository(dsn="postgresql://inventory"))


@router.post("/api/checkout")
async def post_checkout(request: Dict[str, Any]) -> Dict[str, Any]:
    """Reserve stock for one checkout request."""
    return await _service().process(request["sku"], request["quantity"])


@router.get("/inventory/reserve")
async def get_inventory_reserve(sku: str) -> Dict[str, Any]:
    """Read the current stock level for a SKU."""
    stock = await _service().repository.fetch_stock(sku)
    return {"sku": sku, "stock": stock}
