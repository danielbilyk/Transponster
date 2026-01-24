"""
Simple file-based tracker for DM file shares.
Persists data to a JSON file that can be mounted as a Docker volume.
"""
import json
import os
import logging
from pathlib import Path
from typing import Dict, Optional
import asyncio

# Use a persistent directory that can be mounted as a volume
# Default to ./data (works locally), or use TRACKER_DATA_DIR env var (for Docker)
TRACKER_DIR = Path(os.getenv("TRACKER_DATA_DIR", "data"))
TRACKER_FILE = TRACKER_DIR / "dm_file_tracker.json"
tracker_lock = asyncio.Lock()

def is_dm_channel(channel_id: str) -> bool:
    """Check if channel_id is a DM (starts with 'D')."""
    return channel_id and channel_id.startswith("D")

def ensure_tracker_dir():
    """Ensure the tracker directory exists."""
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)

def load_tracker() -> Dict[str, bool]:
    """Load tracking data from JSON file."""
    ensure_tracker_dir()
    if not TRACKER_FILE.exists():
        return {}
    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logging.warning(f"Failed to load tracker file: {e}. Starting with empty tracker.")
        return {}

def save_tracker(data: Dict[str, bool]):
    """Save tracking data to JSON file."""
    ensure_tracker_dir()
    try:
        with open(TRACKER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except IOError as e:
        logging.error(f"Failed to save tracker file: {e}")

async def track_file_share(file_id: str, channel_id: str) -> bool:
    """
    Track whether a file was shared in a DM.
    Returns True if it's a DM, False otherwise.
    """
    is_dm = is_dm_channel(channel_id)
    
    async with tracker_lock:
        tracker_data = load_tracker()
        tracker_data[file_id] = is_dm
        save_tracker(tracker_data)
    
    if is_dm:
        logging.info(f"[{file_id}] Tracked as shared in DM (channel: {channel_id})")
    else:
        logging.info(f"[{file_id}] Tracked as shared in channel (channel: {channel_id})")
    
    return is_dm

async def was_shared_in_dm(file_id: str) -> Optional[bool]:
    """
    Check if a file was shared in a DM.
    Returns True if DM, False if channel, None if not tracked.
    """
    async with tracker_lock:
        tracker_data = load_tracker()
        return tracker_data.get(file_id)

