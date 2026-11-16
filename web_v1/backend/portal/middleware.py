"""A customer session may use only the portal API and signing in or out: every other /api/ path answers 403.
This sits on top of each staff view's own permission checks."""
from django.http import JsonResponse

CUSTOMER_PATHS = ('/api/v1/portal/', '/api/v1/planning/session', '/api/v1/planning/logout',
                  '/api/v1/governance/login', '/api/v1/governance/session')


class CustomerGuard:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if (request.path.startswith('/api/') and user is not None and user.is_authenticated
                and hasattr(user, 'customer_account') and not request.path.startswith(CUSTOMER_PATHS)):
            return JsonResponse({'error': 'This page is not available to customer accounts'}, status=403)
        return self.get_response(request)
