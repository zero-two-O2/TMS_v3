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

    def debug(self, message: str, *args, **kwargs) -> None:
        self._logger.debug(message, *args, **kwargs)

    def info(self, message: str, *args, **kwargs) -> None:
        self._logger.info(message, *args, **kwargs)

    def warning(self, message: str, *args, **kwargs) -> None:
        self._logger.warning(message, *args, **kwargs)

    def error(self, message: str, *args, **kwargs) -> None:
        self._logger.error(message, *args, **kwargs)

    def critical(self, message: str, *args, **kwargs) -> None:
        self._logger.critical(message, *args, **kwargs)

    def exception(self, message: str, *args, **kwargs) -> None:
        self._logger.exception(message, *args, **kwargs)


logger = _Logger()


def get_logger(name: str) -> _Logger:
    """Get a logger instance for a module."""
    return logger