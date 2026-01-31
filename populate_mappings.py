#!/usr/bin/env python3
"""
One-time script to backfill file mappings for existing transcripts.

Searches Slack for .txt files uploaded since 2025-01-01, then attempts to find
matching .docx files on Google Drive by filename stem.

Usage:
    python populate_mappings.py [--dry-run]
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from config import SLACK_BOT_TOKEN
from helpers import get_google_drive_service, get_or_create_shared_drive
from file_mappings import save_file_mapping, _load_mappings

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def list_slack_txt_files(client: WebClient, since_ts: float) -> list[dict]:
    """List all .txt files from Slack since the given timestamp."""
    files = []
    cursor = None

    while True:
        try:
            result = client.files_list(
                ts_from=str(int(since_ts)),
                types="text",
                count=100,
                cursor=cursor
            )

            for f in result.get("files", []):
                name = f.get("name", "")
                if name.lower().endswith(".txt"):
                    files.append({
                        "id": f["id"],
                        "name": name,
                        "created": f.get("created", 0)
                    })

            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        except SlackApiError as e:
            logging.error(f"Slack API error: {e}")
            break

    return files


def search_drive_for_docx(service, shared_drive_id: str, filename_stem: str) -> str | None:
    """Search Google Drive for a .docx file matching the filename stem."""
    try:
        # Search for .docx file with matching name
        query = f"name contains '{filename_stem}' and mimeType='application/vnd.openxmlformats-officedocument.wordprocessingml.document' and trashed=false"

        results = service.files().list(
            q=query,
            spaces='drive',
            fields='files(id, name)',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora='drive',
            driveId=shared_drive_id
        ).execute()

        files = results.get('files', [])

        # Look for exact match (stem matches)
        for f in files:
            drive_stem = Path(f['name']).stem
            if drive_stem.lower() == filename_stem.lower():
                return f['id']

        return None

    except Exception as e:
        logging.error(f"Drive search error for '{filename_stem}': {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Backfill Slack-to-Drive file mappings")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without saving")
    parser.add_argument("--since", default="2025-01-01", help="Start date (YYYY-MM-DD)")
    args = parser.parse_args()

    # Parse start date
    since_date = datetime.strptime(args.since, "%Y-%m-%d")
    since_ts = since_date.timestamp()

    logging.info(f"Looking for .txt files since {args.since}")
    if args.dry_run:
        logging.info("DRY RUN - no changes will be saved")

    # Initialize clients
    slack_client = WebClient(token=SLACK_BOT_TOKEN)
    drive_service = get_google_drive_service()

    if not drive_service:
        logging.error("Could not initialize Google Drive service")
        return 1

    shared_drive_id = get_or_create_shared_drive(drive_service)
    if not shared_drive_id:
        logging.error("Could not find Transponster shared drive")
        return 1

    # Load existing mappings
    existing_mappings = _load_mappings()
    logging.info(f"Found {len(existing_mappings)} existing mappings")

    # Get .txt files from Slack
    txt_files = list_slack_txt_files(slack_client, since_ts)
    logging.info(f"Found {len(txt_files)} .txt files in Slack since {args.since}")

    # Track statistics
    found = 0
    skipped_existing = 0
    not_found = 0

    for txt_file in txt_files:
        slack_id = txt_file["id"]
        name = txt_file["name"]
        stem = Path(name).stem

        # Skip if already mapped
        if slack_id in existing_mappings:
            logging.debug(f"Skipping {name} - already mapped")
            skipped_existing += 1
            continue

        # Search for matching .docx on Drive
        drive_id = search_drive_for_docx(drive_service, shared_drive_id, stem)

        if drive_id:
            logging.info(f"MATCH: {name} (Slack: {slack_id}) -> Drive: {drive_id}")
            found += 1
            if not args.dry_run:
                save_file_mapping(slack_id, drive_id)
        else:
            logging.debug(f"No match found for {name}")
            not_found += 1

    # Summary
    logging.info("=" * 50)
    logging.info(f"Summary:")
    logging.info(f"  Total .txt files: {len(txt_files)}")
    logging.info(f"  Already mapped: {skipped_existing}")
    logging.info(f"  New matches found: {found}")
    logging.info(f"  No match found: {not_found}")

    if args.dry_run:
        logging.info("(Dry run - no changes saved)")
    else:
        logging.info(f"Saved {found} new mappings")

    return 0


if __name__ == "__main__":
    sys.exit(main())
