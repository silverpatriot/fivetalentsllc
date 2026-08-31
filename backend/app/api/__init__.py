from fastapi import APIRouter

from app.api import billing, documents, health, sermons, study, tenants, webhooks_clerk, webhooks_stripe

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(billing.router)
api_router.include_router(tenants.router)
api_router.include_router(sermons.router)
api_router.include_router(documents.router)
api_router.include_router(study.router)
api_router.include_router(webhooks_clerk.router, tags=["webhooks"])
api_router.include_router(webhooks_stripe.router, tags=["webhooks"])
