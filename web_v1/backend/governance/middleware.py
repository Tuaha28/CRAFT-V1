"""Security headers on every API answer: a strict content security policy (the API returns data, never pages)."""

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


class ApiSecurityHeaders:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault('Content-Security-Policy', API_CSP)
        response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        return response
