# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Design Philosophy

**Simplicity for the user above everything else.** The users are non-technical people who just want to paste audio/video into Slack, wait a bit, and get back either a transcript or an error message. That's it. No configuration, no commands, no options to figure out. Any complexity must be hidden from users entirely.

## Project Overview

Transponster is a Slack bot that transcribes audio/video files using the ElevenLabs API and uploads transcripts to Google Drive as Word documents. When users share audio/video files in Slack (DM or channel), the bot automatically downloads, transcribes, and returns the result.

## Running the Bot

```bash
# Local development
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn bot:api --host 0.0.0.0 --port 3000

# Docker
docker build -t transponster-bot .
docker compose up -d
```

For local development, use ngrok to expose port 3000 and update Slack's Event Subscriptions Request URL.

## Architecture

**Entry Point:** `bot.py` - FastAPI app that handles Slack webhook at `/slack/events`

**Core Flow:**
1. `slack_events.py` - Async Slack Bolt app handling `file_shared` and `message` (file_share subtype) events
2. File uploads are batched (3-second window) to handle multiple files uploaded together
3. `helpers.py:transcribe_file()` - Sends audio to ElevenLabs STT API
4. Results uploaded as `.txt` to Slack and `.docx` to Google Drive

**Key Modules:**
- `config.py` - Environment variable loading (Slack tokens, ElevenLabs API key, Google credentials)
- `helpers.py` - ElevenLabs transcription, SRT generation, Google Drive operations, subtitle translation
- `logger_setup.py` - Logging configuration (stdout only)

**Transcription Modes:** Determined by filename keywords:
- Default: `.txt` transcript only
- "subtitles" or "субтитри" in filename: `.srt` only
- "both" or "обидва" in filename: both formats

## Required Environment Variables

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
ELEVENLABS_API_KEY=...
OPENAI_API_KEY=sk-...  # for subtitle translation
GOOGLE_CREDENTIALS_JSON='{"type": "service_account", ...}'  # or GOOGLE_APPLICATION_CREDENTIALS path
SLACK_STARTUP_CHANNEL=C0XXXXXXX  # optional
```

## Debug Mode

For local testing (staging via ngrok), set `DEBUG=true` to disable Google Drive uploads by default. To test Google Drive functionality in debug mode, also set `DEBUG_GDRIVE=true`.

For local development, also set `MAPPINGS_FILE=./data/file_mappings.json` (Docker uses `/app/data/file_mappings.json` by default).

## Supported File Types

`.mp3`, `.wav`, `.mp4`, `.m4a`, `.flac`, `.ogg`, `.aac` (max 1000 MB)

## Translation via Emoji Reactions

Users can translate `.srt` subtitle files or `.txt` transcript files to English by adding a flag emoji reaction (🇬🇧, 🇺🇸, or 🏴󠁧󠁢󠁥󠁮󠁧󠁿) to a message containing the file **within a thread**.

**Supported files:**
- `.srt` subtitles → outputs `{filename}-eng.srt`
- `.txt` transcripts (Transponster format) → outputs `{filename}-eng.txt`

**Flow:**
1. User reacts with English/US/England flag emoji on a message with `.srt` or `.txt` file in a thread
2. Bot downloads the file, parses it deterministically (extracts text, preserves structure)
3. Text blocks are sent to OpenAI for translation
4. Bot reconstructs the file with translated text
5. Uploads translated file to the thread

**Requirements:**
- `OPENAI_API_KEY` environment variable must be set
- Slack app needs `reactions:read` scope
- Only works on messages inside threads (not parent messages)

**Implementation Principle:** LLMs handle translation only—nothing else. All deterministic tasks (parsing structure, preserving timestamps/indices/speakers, reassembling the file) are done in Python. The LLM receives plain text and returns plain text. No JSON, no IDs, no structure for the LLM to mess up.

**Key functions:**
- `helpers.py:parse_srt_content()` - Parses SRT into list of entries (index, timestamp, text)
- `helpers.py:parse_transcript_content()` - Parses Transponster `.txt` into entries (header, text)
- `helpers.py:translate_texts_with_openai()` - Async function, sends text to OpenAI for translation
- `helpers.py:rebuild_srt_with_translations()` - Reassembles SRT from entries + translated texts
- `helpers.py:rebuild_transcript_with_translations()` - Reassembles `.txt` from entries + translated texts

**Google Drive Integration for Translations:**
When translating a `.txt` transcript, the bot also updates the corresponding Google Drive `.docx` document by appending the English translation as a new page. This uses the file mappings system (see below).

## File Mappings (Slack-to-Drive)

The bot maintains a JSON file mapping Slack file IDs to Google Drive file IDs. This enables finding the Drive document when a user requests translation of a `.txt` file.

**File location:** `/app/data/file_mappings.json` (configurable via `MAPPINGS_FILE` env var)

**How it works:**
- When a transcript is uploaded to both Slack and Drive, the mapping is saved
- When translation is requested, the mapping is used to find and update the Drive doc
- If no mapping exists (e.g., older files), translation still works in Slack, but Drive doc isn't updated

**Backfill script:** `populate_mappings.py` can retroactively populate mappings by matching filenames:
```bash
python populate_mappings.py --dry-run  # Preview what would be mapped
python populate_mappings.py            # Actually save mappings
```
