import logging
import os

_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

class _Formatter(logging.Formatter):
	def format(self, record: logging.LogRecord) -> str:
		prefix = f"[{record.levelname}] {record.name}:"
		msg = super().format(record)
		return f"{prefix} {msg}"

_handler = logging.StreamHandler()
_handler.setFormatter(_Formatter("%(message)s"))

logging.basicConfig(level=getattr(logging, _LEVEL, logging.INFO), handlers=[_handler])


def get_logger(name: str) -> logging.Logger:
	logger = logging.getLogger(name)
	logger.setLevel(getattr(logging, _LEVEL, logging.INFO))
	return logger
