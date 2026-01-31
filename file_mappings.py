"""
Persistent mapping between Slack file IDs and Google Drive file IDs.

This module provides a simple JSON-based storage for tracking which Slack files
correspond to which Google Drive documents. Written when transcription completes,
read when translation is requested.
"""

import json
import os
import fcntl
import logging
from pathlib import Path

MAPPINGS_FILE = os.environ.get("MAPPINGS_FILE", "/app/data/file_mappings.json")


def _load_mappings() -> dict:
    """Load mappings from JSON file, returning empty dict if file doesn't exist."""
    path = Path(MAPPINGS_FILE)
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logging.error(f"Failed to load mappings file {MAPPINGS_FILE}: {e}")
        return {}


def _save_mappings(mappings: dict):
    """Save mappings to JSON file with file locking for concurrent access."""
    path = Path(MAPPINGS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                json.dump(mappings, f, indent=2)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except IOError as e:
        logging.error(f"Failed to save mappings file {MAPPINGS_FILE}: {e}")


def save_file_mapping(slack_file_id: str, drive_file_id: str):
    """Store mapping from Slack file ID to Drive file ID."""
    mappings = _load_mappings()
    mappings[slack_file_id] = drive_file_id
    _save_mappings(mappings)
    logging.info(f"Saved file mapping: Slack {slack_file_id} -> Drive {drive_file_id}")


def get_drive_file_id(slack_file_id: str) -> str | None:
    """Get Drive file ID for a Slack file ID, or None if not found."""
    mappings = _load_mappings()
    return mappings.get(slack_file_id)
