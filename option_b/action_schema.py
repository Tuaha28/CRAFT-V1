"""Versioned observational action records; never used by core GA."""
SCHEMA_VERSION = 1
REQUIRED = ("action_id", "action_type", "window", "parent_chromosome_sha256", "operator_diff", "repair_diff", "sampling_seed")
def validate(record):
    missing=[k for k in REQUIRED if k not in record]
    if missing: raise ValueError(f"missing action fields: {missing}")
    return True
