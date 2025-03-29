#!/bin/sh
set -e

# Initialize bot.log if it doesn't exist
if [ ! -f /app/bot.log ]; then
  touch /app/bot.log
fi

exec "$@"