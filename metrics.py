import json
import logging
from pathlib import Path

class MetricsManager:
    """Load, update, and save transcription metrics."""
    def __init__(self, metrics_file):
        self.metrics_file = Path(metrics_file)
        self.metrics = self.load_metrics()

    def load_metrics(self):
        # Load metrics from a JSON file if it exists; otherwise, return default metrics.
        if self.metrics_file.exists():
            try:
                with open(self.metrics_file, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logging.error(f"Error parsing metrics file {self.metrics_file}. Starting with fresh metrics.")
        return {
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

    def save_metrics(self):
        """Save current metrics to the JSON file."""
        self.metrics_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.metrics_file, 'w') as f:
            json.dump(self.metrics, f, indent=2)

    def update_user_metrics(self, user_id, username, file_duration_seconds, success=True, processing_time=None):
        """
        Update per-user and global metrics. 
        All file duration values are in seconds.
        """
        if user_id not in self.metrics["users"]:
            self.metrics["users"][user_id] = {
                "username": username,
                "files_uploaded": 0,
                "total_seconds": 0,
                "file_durations": [],
                "success_rate": {
                    "successful": 0,
                    "failed": 0
                }
            }
        self.metrics["users"][user_id]["files_uploaded"] += 1
        self.metrics["users"][user_id]["total_seconds"] += file_duration_seconds
        self.metrics["users"][user_id]["file_durations"].append(file_duration_seconds)

        if success:
            self.metrics["users"][user_id]["success_rate"]["successful"] += 1
            self.metrics["transcription_success_rate"]["successful"] += 1
        else:
            self.metrics["users"][user_id]["success_rate"]["failed"] += 1
            self.metrics["transcription_success_rate"]["failed"] += 1

        self.metrics["total_files_processed"] += 1
        self.metrics["total_seconds_processed"] += file_duration_seconds
        self.metrics["average_file_length_seconds"]["total_length"] += file_duration_seconds
        self.metrics["average_file_length_seconds"]["count"] += 1

        if processing_time is not None:
            self.metrics["average_processing_time_seconds"]["total_time"] += processing_time
            self.metrics["average_processing_time_seconds"]["count"] += 1

        self.save_metrics()        