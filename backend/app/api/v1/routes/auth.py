from fastapi import APIRouter, Request, Response, status

from app.core.config import get_settings
from app.core.dependencies import AUTH_RESPONSES, Auth, DbSession
from app.schemas.auth import ChangePasswordRequest, LoginRequest, LoginResponse
from app.schemas.error import ErrorResponse
from app.schemas.user import UserRead
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_auth_cookies(response: Response, session_token: str, csrf_token: str) -> None:
    settings = get_settings()
    max_age = settings.session_ttl_hours * 3600
    response.set_cookie(
        settings.session_cookie_name,
        session_token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    # Readable by the frontend so it can echo it in the X-CSRF-Token header.
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    settings = get_settings()
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(
            name, path="/", secure=settings.cookie_secure, samesite="lax"
        )


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={401: {"model": ErrorResponse, "description": "Invalid credentials"}},
)
def login(
    payload: LoginRequest, request: Request, response: Response, db: DbSession
) -> LoginResponse:
    result = auth_service.login(
        db,
        str(payload.email),
        payload.password,
        previous_token=request.cookies.get(get_settings().session_cookie_name),
    )
    _set_auth_cookies(response, result.session_token, result.csrf_token)
    return LoginResponse(
        user=UserRead.model_validate(result.user), csrf_token=result.csrf_token
    )


@router.post(
    "/logout", status_code=status.HTTP_204_NO_CONTENT, responses=AUTH_RESPONSES
)
def logout(ctx: Auth, response: Response, db: DbSession) -> None:
    auth_service.logout(db, ctx)
    _clear_auth_cookies(response)


@router.get("/me", response_model=UserRead, responses=AUTH_RESPONSES)
def me(ctx: Auth) -> UserRead:
    return UserRead.model_validate(ctx.user)


@router.post(
    "/change-password",
    response_model=UserRead,
    responses={
        **AUTH_RESPONSES,
        422: {"model": ErrorResponse, "description": "Wrong or reused password"},
    },
)
def change_password(
    payload: ChangePasswordRequest, ctx: Auth, db: DbSession
) -> UserRead:
    user = auth_service.change_password(
        db, ctx, payload.current_password, payload.new_password
    )
    return UserRead.model_validate(user)
