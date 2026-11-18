class ServiceError(Exception):
    """Business-rule failure with a safe, client-facing message."""

    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(ServiceError):
    status_code = 404


class BusinessRuleError(ServiceError):
    status_code = 422


class ConflictError(ServiceError):
    status_code = 409
