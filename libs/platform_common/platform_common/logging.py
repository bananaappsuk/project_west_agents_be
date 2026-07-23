"""Consistent logging setup for every service.

Call configure_logging("<service>") once at startup, then use
logging.getLogger("<service>.<module>") anywhere. Level via LOG_LEVEL env (default INFO;
set LOG_LEVEL=DEBUG for verbose output).
"""

import logging
import os


def configure_logging(service: str) -> logging.Logger:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        force=True,  # reset handlers so uvicorn --reload doesn't duplicate lines
    )
    return logging.getLogger(service)
