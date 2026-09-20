"""Tests for the ingestion pipeline and event normalizer."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sources import MockObservabilitySource, RawObservabilityEvent
from app.models.observability import ObservabilityEvent
from app.services.normalizer import NormalizationError, ObservabilityNormalizer


class TestObservabilityNormalizer:
    """Test raw event normalization."""

    async def test_normalize_valid_event(self, db_session: AsyncSession) -> None:
        """A valid raw event normalizes into an ObservabilityEvent."""
        raw = RawObservabilityEvent(
            source_type="log",
            source_name="app-logger",
            timestamp=datetime.now(timezone.utc),
            event_type="LOG",
            payload={
                "message": "Health check failed",
                "severity": "ERROR",
                "trace_id": "trace-xyz",
            },
        )
        normalizer = ObservabilityNormalizer(db_session)
        event = await normalizer.normalize(raw, uuid.uuid4())

        assert isinstance(event, ObservabilityEvent)
        assert event.event_type.value == "LOG"
        assert event.source == "log:app-logger"
        assert event.severity.value == "ERROR"
        assert event.trace_id == "trace-xyz"
        db_session.add(event)
        await db_session.flush()

    async def test_reject_unknown_source_type(self, db_session: AsyncSession) -> None:
        """Unknown source types are rejected."""
        raw = RawObservabilityEvent(
            source_type="blah",
            source_name="x",
            timestamp=datetime.now(timezone.utc),
            event_type="LOG",
            payload={},
        )
        normalizer = ObservabilityNormalizer(db_session)
        try:
            await normalizer.normalize(raw, uuid.uuid4())
            assert False, "Should have raised NormalizationError"
        except NormalizationError as e:
            assert e.reason == "UNKNOWN_SOURCE_TYPE"

    async def test_reject_unknown_event_type(self, db_session: AsyncSession) -> None:
        """Unknown event types are rejected."""
        raw = RawObservabilityEvent(
            source_type="log",
            source_name="x",
            timestamp=datetime.now(timezone.utc),
            event_type="NOPE",
            payload={},
        )
        normalizer = ObservabilityNormalizer(db_session)
        try:
            await normalizer.normalize(raw, uuid.uuid4())
            assert False, "Should have raised NormalizationError"
        except NormalizationError as e:
            assert e.reason == "UNKNOWN_EVENT_TYPE"

    async def test_skip_malformed_in_batch(self, db_session: AsyncSession) -> None:
        """Malformed events are skipped without aborting the batch."""
        good = RawObservabilityEvent(
            source_type="metric",
            source_name="prometheus",
            timestamp=datetime.now(timezone.utc),
            event_type="METRIC",
            payload={"metric_name": "cpu", "value": 1.0},
        )
        bad = RawObservabilityEvent(
            source_type="unknown!!",
            source_name="x",
            timestamp=datetime.now(timezone.utc),
            event_type="METRIC",
            payload={},
        )
        normalizer = ObservabilityNormalizer(db_session)
        events = await normalizer.normalize_batch([good, bad], uuid.uuid4())

        assert len(events) == 1
        assert events[0].source == "metric:prometheus"


class TestMockObservabilitySource:
    """Test the mock observability source."""

    async def test_mock_is_healthy(self) -> None:
        source = MockObservabilitySource()
        assert await source.is_healthy()

    async def test_mock_fetches_events(self) -> None:
        source = MockObservabilitySource()
        events = await source.fetch_events(
            datetime.now(timezone.utc), datetime.now(timezone.utc)
        )
        assert len(events) == 1
        assert events[0].source_type == "mock"


class TestMockAIProvider:
    """Test the mock AI provider produces deterministic placeholders."""

    async def test_completion_placeholder(self) -> None:
        from app.services.engines import MockAIProvider

        provider = MockAIProvider()
        result = await provider.complete("analyze this incident")
        assert "[mock-ai]" in result

    async def test_structured_placeholder(self) -> None:
        from app.services.engines import MockAIProvider

        provider = MockAIProvider()
        result = await provider.complete_structured("x", {"type": "object"})
        assert result["mock"] is True
