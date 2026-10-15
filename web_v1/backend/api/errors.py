from rest_framework.views import exception_handler

def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None
    detail = response.data
    message = detail if isinstance(detail, str) else "The request could not be completed. Check the submitted fields and try again."
    response.data = {"error": {"code": getattr(exc, "default_code", "request_error"), "message": message, "details": detail}}
    return response
