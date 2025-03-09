#!/bin/sh
set -e

# Initialize transcription_metrics.json if it doesn't exist or is empty
if [ ! -s /app/transcription_metrics.json ]; then
  cat <<EOF > /app/transcription_metrics.json
{
  "users": {},
  "total_files_processed": 0,
  "total_seconds_processed": 0,
  "transcription_success_rate": {
    "successful": 0,
    "failed": 0
  },
  "average_processing_time_seconds": {
    "total_time": 0,
    "count": 0
  },
  "average_file_length_seconds": {
    "total_length": 0,
    "count": 0
  }
}
EOF
fi

# Initialize bot.log if it doesn't exist
if [ ! -f /app/bot.log ]; then
  touch /app/bot.log
fi

exec "$@"
