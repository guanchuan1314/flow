class ServiceError(Exception):
    """Business-rule violation raised by services; rendered as a JSON error by the API."""

    status_code = 400

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409


class Forbidden(ServiceError):
    status_code = 403


def get_or_404(db, model, ident, label: str | None = None):
    obj = db.get(model, ident)
    if obj is None:
        raise NotFound(f"{label or model.__name__} {ident} not found")
    return obj
