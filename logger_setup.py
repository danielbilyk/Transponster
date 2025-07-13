import logging

LOG_FORMAT = '%(asctime)s [%(levelname)s] %(message)s'
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Clear existing handlers if any
if logger.hasHandlers():
    logger.handlers.clear()

# Console handler (logs to terminal/stdout)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter(LOG_FORMAT)
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)