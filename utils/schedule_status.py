"""Statuses whose committed intervals still reserve shop-floor resources.

The v2 seeder emits Scheduled (future), Running or Delayed (in progress),
and Done (finished). Failed is retained conservatively for legacy unfinished
rows: database.queries also exposes these as outstanding work. Only an
explicit completion/cancellation releases such a reservation.
"""
OCCUPYING_STATUSES = ('Scheduled', 'Running', 'Delayed', 'Failed')

# Of the occupying statuses, those whose work has already BEGUN. The seeder
# derives status by comparing actuals to its own "now":
#   actual_end <= now -> 'Done'; actual_start > now -> 'Scheduled';
#   otherwise -> 'Running'/'Delayed'
# so 'Scheduled' means not started and everything else here means started
# (Failed included: a failed row is unfinished work that did begin).
#
# Used to classify a committed reservation as fixed (under way, immovable) or
# replaceable (merely planned, and being rescheduled by this very run). NOTE
# the label is frozen at SEED time, so a caller rescheduling at a later
# origin must also treat an interval straddling that origin as in flight --
# see database.queries.classify_reservation.
IN_FLIGHT_STATUSES = ('Running', 'Delayed', 'Failed')
