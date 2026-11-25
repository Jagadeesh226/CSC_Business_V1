"""Imports every module that registers task handlers.

The worker imports this at start-up.
"""

from app.workers import (
    ocr_handler,  # noqa: F401 - registers document_ocr
    reminder_jobs,  # noqa: F401 - daily expiry check
)
