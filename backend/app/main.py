from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DataError

from app.api.v1.router import api_router
from app.services.errors import ServiceError

app = FastAPI(
    title="CSC Business CRM API",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health/live")
def health_live():
    return {"status": "ok"}


@app.get("/api/v1/health")
def api_health():
    return {
        "status": "ok",
        "service": "csc-business-crm-api",
    }


@app.exception_handler(ServiceError)
def handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(DataError)
def handle_data_error(_: Request, exc: DataError) -> JSONResponse:
    # Input PostgreSQL cannot store (e.g. a NUL character) is a client error;
    # the request's session is rolled back by get_db. No details are leaked.
    return JSONResponse(
        status_code=422,
        content={"detail": "The request contains characters that cannot be stored."},
    )


app.include_router(api_router)
