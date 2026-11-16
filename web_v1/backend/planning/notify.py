"""Email the factory's managers when an automatic CRAFT plan is ready or failed.

Only automatic runs send email (a manager who started a plan by hand is already looking).
The email holds the reason, a few numbers and one link; approval happens only in the app.
Sending never raises: the outcome is written to the run's history (PlanEvent).
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives
from django.utils.html import escape
from .services import event


def recipients(run):
    """Emails of active users who can approve plans (request_plan) for this factory, or for all factories."""
    found = []
    for user in User.objects.filter(is_active=True).exclude(email='').select_related('floor_profile'):
        if user.username == 'craft_auto' or not user.has_perm('planning.request_plan'):
            continue
        profile = getattr(user, 'floor_profile', None)
        if profile is not None and profile.factory_id is not None and profile.factory_id != run.factory_id:
            continue
        found.append(user.email)
    return sorted(set(found))


def _finishes(run):
    try:
        rows = json.loads((Path(run.out_dir) / 'digest.json').read_text(encoding='utf-8')).get('order_rows', ())
    except (OSError, ValueError, TypeError):
        return {}
    return {r['order_id']: (r.get('methods', {}).get('CRAFT_V3') or {}).get('finish_day') for r in rows}


def _date(run, day):
    try:
        return (datetime.fromisoformat(run.summary.get('reference_time') or run.dataset.reference_time)
                + timedelta(days=float(day))).strftime('%d %b %Y').lstrip('0')
    except (TypeError, ValueError):
        return None


def _body(run):
    factory, link = run.factory_id, f'{settings.APP_BASE_URL.rstrip("/")}/runs/{run.pk}'
    reason = (run.trigger or {}).get('reason') or 'Changes on the floor'
    data_date = (run.dataset.reference_time or '')[:10]
    from .autoplan import factory_time
    times = f'Requested {factory_time(run.created_at):%d %b %H:%M}' +         (f', finished {factory_time(run.finished_at):%d %b %H:%M}' if run.finished_at else '')
    if run.status != 'SUCCEEDED':
        subject = f'CRAFT could not make the plan for Factory {factory}'
        lines = [f'CRAFT tried to re-plan Factory {factory} because: {reason}.',
                 'The plan could not be made. Open the app to see what happened and start a new plan.',
                 times + '.', '', f'Open in CRAFT: {link}']
        return subject, lines
    subject = f'CRAFT has a new plan for Factory {factory}: {reason}'
    mine, before = run.summary.get('craft_v3') or {}, ((run.parent.summary if run.parent else {}) or {}).get('craft_v3') or {}
    lines = [f'CRAFT made a new plan for Factory {factory} because: {reason}.', '']
    if mine.get('late') is not None and before.get('late') is not None:
        lines.append(f'Late orders: {before["late"]} → {mine["late"]}')
    old, new = _finishes(run.parent) if run.parent else {}, _finishes(run)
    shared = [o for o in new if o in old and new[o] is not None and old[o] is not None]
    later = sum(1 for o in shared if new[o] > old[o] + 1e-6)
    earlier = sum(1 for o in shared if new[o] < old[o] - 1e-6)
    if later:
        lines.append(f'{later} order{"s" if later > 1 else ""} finish{"" if later > 1 else "es"} later')
    if earlier:
        lines.append(f'{earlier} order{"s" if earlier > 1 else ""} finish{"" if earlier > 1 else "es"} earlier')
    last = _date(run, mine.get('makespan_days')) if mine.get('makespan_days') is not None else None
    if last:
        lines.append(f'Last order finishes {last}')
    lines += ['', f'Planned from the factory data of {data_date}, as if it stays that way for the whole plan.',
              times + '.', '', 'Review the plan and approve it or send it back in CRAFT:', link]
    return subject, lines


def run_finished(run):
    """'SENT', 'NO_RECIPIENTS', 'FAILED' or 'SKIPPED' (not an automatic run). Never raises."""
    if (run.trigger or {}).get('source') != 'AUTO':
        return 'SKIPPED'
    to = recipients(run)
    if not to:
        event(run, None, 'EMAIL_NO_RECIPIENTS', 'No manager has an email address, so nobody was told')
        return 'NO_RECIPIENTS'
    try:
        subject, lines = _body(run)
        text = '\n'.join(lines)
        html = '<div style="font-family:sans-serif">' + ''.join(
            f'<p><a href="{escape(line)}">Review the plan</a></p>' if line.startswith('http')
            else f'<p>{escape(line)}</p>' if line else '' for line in lines) + '</div>'
        message = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, to)
        message.attach_alternative(html, 'text/html')
        message.send()
    except Exception as exc:  # SMTP down, bad settings: record it, never fail the run
        event(run, None, 'EMAIL_FAILED', f'The email to the managers could not be sent: {type(exc).__name__}')
        return 'FAILED'
    event(run, None, 'EMAIL_SENT', f'Emailed {len(to)} manager{"s" if len(to) > 1 else ""}')
    return 'SENT'
