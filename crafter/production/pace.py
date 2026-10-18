"""Manager pace setting -> CRAFT settings (three levels, manager decides).

SLOW   just-in-time: after delivery, finish close to due minus a buffer,
       concentrate work on few workers, crews of at most 2.
NORMAL economy (default): after delivery, fewest distinct workers, crews
       only where they help delivery (up to 4).
FAST   after delivery, finish everything as early as possible, crews up to 4.

Delivery goals (late orders, total and maximum tardiness) always come first,
so a pace never trades away on-time delivery.
"""

PACES = {
    "SLOW": {"objective_mode": "SLOW", "max_crew": 2, "jit_buffer_days": 2},
    "NORMAL": {"objective_mode": "ECONOMY", "max_crew": 4, "jit_buffer_days": 2},
    "FAST": {"objective_mode": "FAST", "max_crew": 4, "jit_buffer_days": 2},
}


def pace_settings(pace="NORMAL"):
    try:
        return dict(PACES[str(pace).upper()])
    except KeyError:
        raise ValueError(f"PACE_INVALID:{pace}") from None


def pace_environment(pace="NORMAL"):
    """Environment variables CRAFT's objective layer reads for this pace."""
    settings = pace_settings(pace)
    return {"CRAFT_OBJECTIVE_MODE": settings["objective_mode"],
            "CRAFT_JIT_BUFFER_DAYS": str(settings["jit_buffer_days"])}
