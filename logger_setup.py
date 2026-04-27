import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = '%(asctime)s [%(levelname)s] %(message)s'
LOG_FILE = Path("/app/bot.log")

logger = logging.getLogger()
logger.setLevel(logging.INFO)

if logger.hasHandlers():
    logger.handlers.clear()

formatter = logging.Formatter(LOG_FORMAT)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

try:
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=2, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
except (PermissionError, FileNotFoundError) as e:
    logger.warning(f"Could not attach file handler at {LOG_FILE}: {e}")