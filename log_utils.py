import logging
import os


def _resolve_log_level() -> int:
    level_name = os.environ.get("DOUYIN_LOG_LEVEL", "ERROR").strip().upper()
    return getattr(logging, level_name, logging.ERROR)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(_resolve_log_level())
    logger.propagate = False
    return logger
