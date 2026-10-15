import io
import logging
import os
import sys
from utils.config import LOG_DIR

# Ensure log directory exists
os.makedirs(LOG_DIR, exist_ok=True)

def get_logger(name: str, log_file: str, level=logging.INFO) -> logging.Logger:
    """
    Creates a custom logger that outputs to both the console and a specific log file.
    """
    logger = logging.getLogger(name)
    
    # Prevent adding multiple handlers if logger is called multiple times
    if not logger.handlers:
        logger.setLevel(level)
        formatter = logging.Formatter(
            '%(asctime)s - [%(levelname)s] - %(name)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # File Handler
        file_path = os.path.join(LOG_DIR, log_file)
        file_handler = logging.FileHandler(file_path, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # Console Handler
        # NOTE: log messages in this project contain emoji (✅/⚠️/🚨). On
        # Windows sys.stderr defaults to the cp1252 code page, which cannot
        # encode them — logging then swallows a UnicodeEncodeError per call
        # and prints "--- Logging error ---" instead of the message. Wrap the
        # stream in UTF-8 with errors='replace' so a console that can't render
        # a glyph degrades to '?' rather than losing the whole log line.
        # (The FileHandler above already pins encoding='utf-8'.)
        try:
            console_stream = io.TextIOWrapper(
                sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
            )
        except (AttributeError, ValueError):
            # sys.stderr has no .buffer (e.g. already wrapped, or redirected
            # to something exotic) — fall back to it directly.
            console_stream = sys.stderr
        console_handler = logging.StreamHandler(console_stream)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger