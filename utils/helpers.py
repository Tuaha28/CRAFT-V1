import re
from datetime import datetime, timedelta
from utils.config import SHIFT_START_HOUR, SHIFT_END_HOUR

def calculate_working_minutes(start_time: datetime, end_time: datetime) -> float:
    """
    Calculates the exact duration between two timestamps in minutes, 
    strictly accounting for factory shift hours (e.g., 06:00 to 22:00).
    Ignores time elapsed during off-shift hours.
    """
    if start_time >= end_time:
        return 0.0

    current = start_time
    total_minutes = 0.0

    while current < end_time:
        # If current time is before shift start, jump to shift start
        if current.hour < SHIFT_START_HOUR:
            current = current.replace(hour=SHIFT_START_HOUR, minute=0, second=0, microsecond=0)
            if current >= end_time:
                break
        
        # If current time is after shift end, jump to next day's shift start
        elif current.hour >= SHIFT_END_HOUR:
            current += timedelta(days=1)
            current = current.replace(hour=SHIFT_START_HOUR, minute=0, second=0, microsecond=0)
            if current >= end_time:
                break
        
        else:
            # Calculate time until either the end_time or the end of the current shift
            next_boundary = min(
                end_time,
                current.replace(hour=SHIFT_END_HOUR, minute=0, second=0, microsecond=0)
            )
            total_minutes += (next_boundary - current).total_seconds() / 60.0
            current = next_boundary

    return total_minutes

def clean_text_for_rag(text: str) -> str:
    """
    Cleans unstructured database text (e.g., fabric names, SKU descriptions) 
    so the RAG LLM can accurately embed and retrieve stock intelligence.
    """
    if not isinstance(text, str):
        return ""
    # Remove special characters but keep alphanumeric and spaces
    cleaned = re.sub(r'[^a-zA-Z0-9\s]', ' ', text)
    # Remove extra whitespace
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned.lower()

def normalize_sam(sam_value: float, worker_efficiency: float = 1.0) -> float:
    """
    Adjusts the Standard Minute Value (SMV/SAM) based on worker efficiency.
    (Objective 6: Fuzzy Logic / Human Factors setup).
    """
    if worker_efficiency <= 0:
        worker_efficiency = 0.1 # Prevent division by zero
    return sam_value / worker_efficiency