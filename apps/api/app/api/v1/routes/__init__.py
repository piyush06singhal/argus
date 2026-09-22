"""ARGUS V1 Routes."""

from fastapi import APIRouter

from app.api.v1.routes.anomalies import router as anomalies_router
from app.api.v1.routes.causal import router as causal_router
from app.api.v1.routes.code import router as code_router
from app.api.v1.routes.components import router as components_router
from app.api.v1.routes.deployments import router as deployments_router
from app.api.v1.routes.environments import router as environments_router
from app.api.v1.routes.fix import router as fix_router
from app.api.v1.routes.graph import router as graph_router
from app.api.v1.routes.incidents import router as incidents_router
from app.api.v1.routes.ingestion import router as ingestion_router
from app.api.v1.routes.metrics_export import router as metrics_router
from app.api.v1.routes.observability import router as observability_router
from app.api.v1.routes.otlp import router as otlp_router
from app.api.v1.routes.projects import router as projects_router
from app.api.v1.routes.reliability import router as reliability_router
from app.api.v1.routes.remediation import router as remediation_router
from app.api.v1.routes.reproduction import router as reproduction_router

api_v1_router = APIRouter(prefix="/api/v1")

api_v1_router.include_router(projects_router)
api_v1_router.include_router(environments_router)
api_v1_router.include_router(components_router)
api_v1_router.include_router(observability_router)
api_v1_router.include_router(incidents_router)
api_v1_router.include_router(deployments_router)
api_v1_router.include_router(ingestion_router)
api_v1_router.include_router(otlp_router)
api_v1_router.include_router(metrics_router)
api_v1_router.include_router(graph_router)
api_v1_router.include_router(anomalies_router)
api_v1_router.include_router(causal_router)
api_v1_router.include_router(reproduction_router)
api_v1_router.include_router(code_router)
api_v1_router.include_router(fix_router)
api_v1_router.include_router(reliability_router)
api_v1_router.include_router(remediation_router)
