"""Structured single-line logging: ts=<utc> level=<..> component=<..> msg=... k=v ...

Plain stdlib logging under systemd (journald captures stdout); no external deps.
"""
from __future__ import annotations

import logging
import sys

from .clock import iso_utc


class _KVFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"ts={iso_utc()} level={record.levelname} component={record.name} msg={record.getMessage()!r}"
        extras = getattr(record, "kv", None)
        if extras:
            base += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += " exc=" + repr(self.formatException(record.exc_info))
        return base


def get_logger(component: str) -> logging.Logger:
    logger = logging.getLogger(component)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_KVFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def kv(**kwargs) -> dict:
    """Usage: log.info("stored item", extra=kv(item_id=..., revision=...))"""
    return {"kv": kwargs}
