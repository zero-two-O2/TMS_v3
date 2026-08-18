"""
V3 Core Logging Module

Simple logging wrapper using Python's standard logging.
"""

from __future__ import annotations

import logging
from pathlib import Path


class _Logger:
    def __init__(self):
        self._logger = logging.getLogger("thermal_monitor")
        if self._logger.handlers:
            return
        self._logger.setLevel(logging.INFO)
        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
        )
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        self._logger.addHandler(console_handler)
        self._logger.propagate = False

    def debug(self, message: str) -> None:
        self._logger.debug(message)

    def info(self, message: str) -> None:
        self._logger.info(message)

    def warning(self, message: str) -> None:
        self._logger.warning(message)

    def error(self, message: str) -> None:
        self._logger.error(message)

    def critical(self, message: str) -> None:
        self._logger.critical(message)

    def exception(self, message: str) -> None:
        self._logger.exception(message)


logger = _Logger()


def get_logger(name: str) -> _Logger:
    """Get a logger instance for a module."""
    return logger