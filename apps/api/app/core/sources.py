"""ARGUS Observability Source Abstraction."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List


@dataclass
class RawObservabilityEvent:
    """A raw observability event from an external source."""

    source_type: str
    source_name: str
    timestamp: datetime
    event_type: str
    payload: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)


class ObservabilitySource(abc.ABC):
    """Provider-neutral interface for observability sources."""

    name: str = "base"

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish connection to the source."""
        raise NotImplementedError

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Close connection to the source."""
        raise NotImplementedError

    @abc.abstractmethod
    async def fetch_events(self, start_time: datetime, end_time: datetime) -> List[RawObservabilityEvent]:
        """Fetch events from the source between the given timestamps."""
        raise NotImplementedError

    @abc.abstractmethod
    async def is_healthy(self) -> bool:
        """Check if the source is healthy."""
        raise NotImplementedError


class MockObservabilitySource(ObservabilitySource):
    """A mock source that generates deterministic sample data for development/testing."""

    name = "mock"

    async def connect(self) -> None:
        """Mock connect - nothing to do."""
        pass

    async def disconnect(self) -> None:
        """Mock disconnect - nothing to do."""
        pass

    async def is_healthy(self) -> bool:
        """Mock is always healthy."""
        return True

    async def fetch_events(self, start_time: datetime, end_time: datetime) -> List[RawObservabilityEvent]:
        """Generate deterministic sample events."""
        # Return a single simple event for testing purposes.
        # Real sample data is seeded via the seed_data.py script.
        return [
            RawObservabilityEvent(
                source_type="mock",
                source_name="mock-source",
                timestamp=start_time,
                event_type="SYSTEM_EVENT",
                payload={
                    "message": "Mock observability event",
                    "component": "mock-component",
                },
                metadata={"mock": True},
            )
        ]


class MockAIProvider:
    """Mock AI provider for testing - no real AI capabilities."""

    async def complete(self, prompt: str) -> str:
        """Return a deterministic placeholder response."""
        return f"[mock-response] received prompt: {prompt[:100]}..."

    async def complete_structured(self, prompt: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        """Return a deterministic placeholder structured response."""
        return {"mock": True, "prompt_processed": True}