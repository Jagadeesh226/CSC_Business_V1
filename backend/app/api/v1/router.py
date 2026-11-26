from fastapi import APIRouter

from app.api.v1.routes import (
    auth,
    catalogue,
    customers,
    documents,
    fees,
    jobs,
    payments,
    queues,
    receipts,
    reminders,
    reports,
    tasks,
    users,
    vehicle_intake,
    vehicle_lookups,
    vehicles,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(jobs.router)
api_router.include_router(fees.router)
api_router.include_router(customers.router)
api_router.include_router(catalogue.router)
api_router.include_router(documents.router)
api_router.include_router(tasks.router)
api_router.include_router(queues.router)
api_router.include_router(payments.router)
api_router.include_router(receipts.router)
api_router.include_router(reports.router)
api_router.include_router(vehicles.router)
api_router.include_router(vehicle_intake.router)
api_router.include_router(reminders.router)
api_router.include_router(vehicle_lookups.router)
