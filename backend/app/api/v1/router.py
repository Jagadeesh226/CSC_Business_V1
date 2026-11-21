from fastapi import APIRouter

from app.api.v1.routes import (
    auth,
    catalogue,
    customers,
    documents,
    fees,
    jobs,
    users,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(jobs.router)
api_router.include_router(fees.router)
api_router.include_router(customers.router)
api_router.include_router(catalogue.router)
api_router.include_router(documents.router)
