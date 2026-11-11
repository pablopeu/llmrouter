"""Logging local acotado y privado; el stdout del proxy queda en journald."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def get_logger(name):
    logger = logging.getLogger(name)
    if not logger.handlers:
        directory = Path(__file__).resolve().parent / "logs"
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        path = directory / f"{name}.log"
        handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3)
        path.chmod(0o600)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
