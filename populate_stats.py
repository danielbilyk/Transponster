#!/usr/bin/env python3
"""
Backfill stats.db from Google Drive transcripts.

Scans the Transponster shared drive for .docx files created in the given year,
resolves the owner folder name as the username, and inserts records into stats.db.

Usage:
    python populate_stats.py [--dry-run] [--year 2026]
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import get_google_drive_service, get_or_create_shared_drive
from stats import init_db, record_transcription, _get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def list_drive_transcripts(service, shared_drive_id: str, year: int) -> list[dict]:
    """List all .docx files in the shared drive created in the given year."""
    query = (
        f"mimeType='application/vnd.openxmlformats-officedocument.wordprocessingml.document'"
        f" and trashed=false"
        f" and createdTime >= '{year}-01-01T00:00:00'"
        f" and createdTime < '{year + 1}-01-01T00:00:00'"
    )

    files = []
    page_token = None

    while True:
        resp = service.files().list(
            q=query,
            spaces="drive",
            fields="nextPageToken, files(id, name, createdTime, parents)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="drive",
            driveId=shared_drive_id,
            pageSize=200,
            pageToken=page_token,
        ).execute()

        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return files


def resolve_top_level_folder(service, folder_id: str, shared_drive_id: str, cache: dict) -> str:
    """Walk up the folder tree to find the top-level folder name (directly under the shared drive)."""
    if folder_id in cache:
        return cache[folder_id]

    original_id = folder_id
    chain = []

    while True:
        if folder_id in cache:
            name = cache[folder_id]
            for fid in chain:
                cache[fid] = name
            cache[original_id] = name
            return name

        try:
            item = service.files().get(
                fileId=folder_id,
                fields="name, parents",
                supportsAllDrives=True,
            ).execute()
        except Exception as e:
            logging.warning(f"Could not resolve folder {folder_id}: {e}")
            for fid in chain:
                cache[fid] = "unknown"
            cache[original_id] = "unknown"
            return "unknown"

        parents = item.get("parents", [])
        if not parents or parents[0] == shared_drive_id:
            name = item.get("name", "unknown")
            for fid in chain:
                cache[fid] = name
            cache[original_id] = name
            cache[folder_id] = name
            return name

        chain.append(folder_id)
        folder_id = parents[0]


def main():
    parser = argparse.ArgumentParser(description="Backfill stats from Google Drive")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without writing")
    parser.add_argument("--year", type=int, default=2026, help="Year to backfill (default: 2026)")
    args = parser.parse_args()

    if "STATS_DB" not in os.environ:
        os.environ["STATS_DB"] = "./data/stats.db"

    init_db()

    drive_service = get_google_drive_service()
    if not drive_service:
        logging.error("Could not initialize Google Drive service")
        return 1

    shared_drive_id = get_or_create_shared_drive(drive_service)
    if not shared_drive_id:
        logging.error("Could not find Transponster shared drive")
        return 1

    logging.info(f"Scanning Drive for .docx files created in {args.year}...")
    files = list_drive_transcripts(drive_service, shared_drive_id, args.year)
    logging.info(f"Found {len(files)} .docx files")

    # Check what's already in the DB to avoid duplicates
    conn = _get_conn()
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT filename FROM transcriptions WHERE timestamp LIKE ?",
            (f"{args.year}-%",),
        ).fetchall()
    }

    folder_cache: dict[str, str] = {}
    inserted = 0
    skipped = 0

    for f in files:
        name = f["name"]
        # Skip translated/cleaned versions
        stem_lower = name.lower()
        if stem_lower.endswith("-eng.docx") or stem_lower.endswith("-clean.docx"):
            continue

        # The original audio filename isn't stored on Drive, but the docx name
        # mirrors the audio stem. Use it as-is.
        if name in existing:
            skipped += 1
            continue

        parent_id = f.get("parents", [None])[0]
        username = resolve_top_level_folder(drive_service, parent_id, shared_drive_id, folder_cache) if parent_id else "unknown"

        created = f.get("createdTime", "")
        # createdTime is like "2026-03-15T14:22:33.000Z" — trim to ISO
        timestamp = created.replace("Z", "").split(".")[0] if created else ""

        logging.info(f"  {name} | {username} | {timestamp}")

        if not args.dry_run:
            record_transcription(
                user_id="backfill",
                username=username,
                channel_id="",
                filename=name,
                mode="txt_only",
                file_size=0,
                timestamp=timestamp,
            )
        inserted += 1

    logging.info("=" * 50)
    logging.info(f"Year: {args.year}")
    logging.info(f"Total .docx files on Drive: {len(files)}")
    logging.info(f"Skipped (already in DB): {skipped}")
    logging.info(f"{'Would insert' if args.dry_run else 'Inserted'}: {inserted}")
    if args.dry_run:
        logging.info("(Dry run — no changes saved)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
