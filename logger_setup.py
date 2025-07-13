import logging

LOG_FORMAT = '%(asctime)s [%(levelname)s] %(message)s'
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Clear existing handlers if any
if logger.hasHandlers():
    logger.handlers.clear()

# Console handler (logs to terminal)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter(LOG_FORMAT)
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

# File handler (logs to /app/logs/bot.log; creates file if not exists)
file_handler = logging.FileHandler("/app/logs/bot.log")
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter(LOG_FORMAT)
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)