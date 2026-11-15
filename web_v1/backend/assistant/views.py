from rest_framework.decorators import api_view, throttle_classes
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from . import router


class PerMinute(UserRateThrottle):
    scope = 'ask_minute'
    rate = '12/min'


class PerDay(UserRateThrottle):
    scope = 'ask_day'
    rate = '300/day'


@api_view(['GET'])
def status(request):
    """Whether questions are read by the AI model (a key is set) or only by the app's own matching."""
    return Response({'ai': bool(router.api_key())})


@api_view(['POST'])
@throttle_classes([PerMinute, PerDay])
def route(request):
    question = request.data.get('question') if isinstance(request.data, dict) else None
    if not isinstance(question, str) or not question.strip():
        return Response({'error': 'A question is required'}, status=400)
    question = ' '.join(question.split())[:router.MAX_QUESTION]
    try:
        return Response({'route': router.route(question)})
    except router.AssistantUnavailable as exc:
        return Response({'error': str(exc)}, status=503)
