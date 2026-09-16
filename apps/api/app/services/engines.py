"""ARGUS Future Engine Interfaces.

These are conceptual interfaces for future phases. They define contracts
but contain NO implementations. Do not implement advanced intelligence here.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID


# ---------------------------------------------------------------------------
# Future Engine Interface Contracts
# ---------------------------------------------------------------------------


@dataclass
class Anomaly:
    """An anomalous behavior detected in observability data."""
    id: str
    component_id: Optional[UUID] = None
    metric_name: Optional[str] = None
    severity: str = "unknown"
    score: float = 0.0
    description: str = ""
    detected_at: datetime = field(default_factory=datetime.now)


@dataclass
class RootCauseHypothesis:
    """A probable root cause hypothesis."""
    id: str
    component_id: Optional[UUID] = None
    confidence: float = 0.0
    evidence_ids: List[str] = field(default_factory=list)
    description: str = ""
    proposed_fix: Optional[str] = None


class AnomalyDetector(abc.ABC):
    """Detects anomalies in observability data.

    Future Phase 3. Not implemented in Phase 0.
    """
    async def detect(self, events: List[Any]) -> List[Anomaly]:
        raise NotImplementedError("AnomalyDetector is a future Phase 3 interface")


class IncidentCorrelator(abc.ABC):
    """Correlates anomalies into incidents.

    Future Phase 3. Not implemented in Phase 0.
    """
    async def correlate(self, anomalies: List[Anomaly]) -> Optional[UUID]:
        raise NotImplementedError("IncidentCorrelator is a future Phase 3 interface")


class RootCauseAnalyzer(abc.ABC):
    """Analyzes evidence to propose root causes.

    Future Phase 4. Not implemented in Phase 0.
    """
    async def analyze(self, incident_id: UUID) -> List[RootCauseHypothesis]:
        raise NotImplementedError("RootCauseAnalyzer is a future Phase 4 interface")


class CausalAnalysisEngine(abc.ABC):
    """Evaluates causal relationships between evidence.

    Future Phase 4. Not implemented in Phase 0.
    """
    async def evaluate(self, cause_id: str, effect_id: str) -> float:
        raise NotImplementedError("CausalAnalysisEngine is a future Phase 4 interface")


class FailureReproductionEngine(abc.ABC):
    """Reconstructs failures safely in an isolated environment.

    Future Phase 5. Not implemented in Phase 0.
    """
    async def reproduce(self, incident_id: UUID) -> Dict[str, Any]:
        raise NotImplementedError("FailureReproductionEngine is a future Phase 5 interface")


class CodeAnalyzer(abc.ABC):
    """Analyzes code to find fault-relevant code paths.

    Future Phase 6. Not implemented in Phase 0.
    """
    async def analyze(self, repository_id: UUID, commit_sha: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError("CodeAnalyzer is a future Phase 6 interface")


class FixGenerator(abc.ABC):
    """Generates candidate code changes for verified root causes.

    Future Phase 7. Not implemented in Phase 0.
    """
    async def generate_fix(self, hypothesis: RootCauseHypothesis) -> List[Dict[str, Any]]:
        raise NotImplementedError("FixGenerator is a future Phase 7 interface")


class PatchVerifier(abc.ABC):
    """Verifies whether proposed changes actually fix the issue.

    Future Phase 7. Not implemented in Phase 0.
    """
    async def verify(self, fix_id: str) -> Dict[str, Any]:
        raise NotImplementedError("PatchVerifier is a future Phase 7 interface")


class ReliabilityPredictor(abc.ABC):
    """Predicts reliability risks from historical behavior.

    Future Phase 8. Not implemented in Phase 0.
    """
    async def predict(self, project_id: UUID) -> Dict[str, Any]:
        raise NotImplementedError("ReliabilityPredictor is a future Phase 8 interface")


class RemediationEngine(abc.ABC):
    """Executes approved remediations safely under policy.

    Future Phase 9. Not implemented in Phase 0.
    """
    async def propose(self, incident_id: UUID) -> List[Dict[str, Any]]:
        raise NotImplementedError("RemediationEngine is a future Phase 9 interface")


class LearningEngine(abc.ABC):
    """Learns from historical incidents to improve future analysis.

    Future Phase 10. Not implemented in Phase 0.
    """
    async def learn(self, incident_id: UUID) -> None:
        raise NotImplementedError("LearningEngine is a future Phase 10 interface")


# ---------------------------------------------------------------------------
# AI Provider Abstraction
# ---------------------------------------------------------------------------


class AIModelProvider(abc.ABC):
    """Provider-neutral interface for AI model access.

    Supports conceptual operations for future use.
    Phase 0 uses MockAIProvider only - no paid API required.
    """

    @abc.abstractmethod
    async def complete(self, prompt: str, **kwargs: Any) -> str:
        """Generate text from a prompt."""
        raise NotImplementedError

    @abc.abstractmethod
    async def complete_structured(self, prompt: str, response_schema: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        """Generate structured output against a schema."""
        raise NotImplementedError

    @abc.abstractmethod
    async def embed(self, text: str) -> List[float]:
        """Generate embeddings for text (future use)."""
        raise NotImplementedError


class MockAIProvider(AIModelProvider):
    """Mock AI provider for testing.

    Returns deterministic placeholder responses. No actual AI inference.
    """

    name = "mock"

    async def complete(self, prompt: str, **kwargs: Any) -> str:
        return f"[mock-ai] deterministic placeholder: {prompt[:80]}…"

    async def complete_structured(self, prompt: str, response_schema: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        return {"mock": True, "note": "Structured output not available from mock provider"}

    async def embed(self, text: str) -> List[float]:
        return [0.0, 0.0, 0.0]