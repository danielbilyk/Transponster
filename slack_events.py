import os
import re
import asyncio
import logging
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import requests

import aiohttp
import aiofiles
from slack_bolt.async_app import AsyncApp

from config import SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, DEBUG, DEBUG_GDRIVE
from srt_polish import is_korotulka_filename
from helpers import (
    is_audio_or_video, file_too_large, SUPPORTED_EXTENSIONS,
    transcribe_file, get_thread_ts, cleanup_temp_file,
    create_srt_from_json, create_transcript,
    get_google_drive_service, find_or_create_folder, upload_as_google_doc,
    get_or_create_shared_drive, update_docx_with_translation, update_docx_with_cleanup,
    update_docx_with_ukrainian,
    parse_srt_content, translate_texts_with_openai, rebuild_srt_with_translations,
    parse_transcript_content, rebuild_transcript_with_translations,
    clean_texts_with_openai
)
from file_mappings import save_file_mapping, get_drive_file_id
from stats import record_transcription

# --- Async Setup ---
blocking_task_executor = ThreadPoolExecutor(max_workers=os.cpu_count() or 4)
app = AsyncApp(
    token=SLACK_BOT_TOKEN,
    signing_secret=SLACK_SIGNING_SECRET
)
aiohttp_session = None
batch_lock = asyncio.Lock()
upload_batch_tasks = {}
processed_file_ids = set()
BATCH_WINDOW_SECONDS = 3.0
MAX_TRANSCRIPTION_RETRIES = 2
RETRY_DELAY_SECONDS = 3

def _extract_drive_file_id(drive_url: str) -> str | None:
    """Extract Google Drive file ID from a webViewLink URL."""
    # URLs can be like:
    # https://drive.google.com/file/d/{id}/view?...
    # https://docs.google.com/document/d/{id}/edit?...
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', drive_url)
    return match.group(1) if match else None

# --- Helper Functions ---
def get_file_word(count: int) -> str:
    """Ukrainian pluralization for 'file'."""
    if count % 10 == 1 and count % 100 != 11:
        return "файл"
    if 2 <= count % 10 <= 4 and (count % 100 < 10 or count % 100 >= 20):
        return "файли"
    return "файлів"

def is_text_file(filename: str) -> bool:
    """Check if file is a text/markdown file that shouldn't be transcribed."""
    return filename.lower().endswith((".txt", ".md"))

async def resolve_username(user_id: str, client) -> str:
    """Display name for stats, falling back to the raw ID if Slack won't say."""
    try:
        user_info = await client.users_info(user=user_id)
        return user_info['user']['profile']['display_name'] or user_info['user']['name']
    except Exception:
        return user_id

async def get_aiohttp_session():
    global aiohttp_session
    if aiohttp_session is None or aiohttp_session.closed:
        aiohttp_session = aiohttp.ClientSession()
    return aiohttp_session

async def download_file_streamed(url: str, local_path: Path, token: str):
    headers = {"Authorization": f"Bearer {token}"}
    session = await get_aiohttp_session()
    async with session.get(url, headers=headers) as response:
        response.raise_for_status()
        async with aiofiles.open(local_path, "wb") as f:
            async for chunk in response.content.iter_chunked(8192):
                await f.write(chunk)

async def run_transcription(local_path: Path, language_code: str | None = None) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        blocking_task_executor,
        partial(transcribe_file, str(local_path), language_code=language_code),
    )

async def transcribe_with_retries(local_path: Path, file_name: str, user_id: str, channel_id: str,
                                  thread_ts: str, client, language_code: str | None = None):
    """
    Transcribe, retrying on 429/5xx. Posts its own progress and give-up messages.
    Returns the transcription result, or None if every attempt failed.
    """
    for attempt in range(MAX_TRANSCRIPTION_RETRIES):
        try:
            return await run_transcription(local_path, language_code=language_code)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 429 or (status is not None and 500 <= status < 600):
                if attempt < MAX_TRANSCRIPTION_RETRIES - 1:
                    logging.warning(f"Attempt {attempt + 1} failed for {file_name}: {e}. Retrying...")
                    error_message = (
                        f":expressionless: Сорі, щось пішло не так з файлом `{file_name}`. Помилка:\n\n"
                        f"```{e}```\n"
                        f"Я спробую ще раз і відпішу тобі."
                    )
                    await client.chat_postMessage(channel=channel_id, text=error_message, thread_ts=thread_ts)
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                else:
                    logging.error(f"All retries failed for {file_name}: {e}.")
                    final_error_message = (
                        f":pensive: Сорі, я все одно не зміг обробити `{file_name}`.\n\n"
                        f"<@{user_id}>, подивись у чому проблема, як матимеш можливість.")
                    await client.chat_postMessage(channel=channel_id, text=final_error_message, thread_ts=thread_ts)
                    return None
            else:
                raise  # Re-raise other HTTP errors to be caught by the generic handler
    return None

async def generate_and_upload_results(mode: str, base_filename: str, result_data: dict, file_info: dict, user_id: str, channel_id: str, thread_ts: str, client, batch_context=None, polish: bool = False):
    temp_files_to_clean = []

    try:
        if mode in ("srt_only", "both"):
            logging.info(f"[{file_info['id']}] 6a: Generating .srt file.")
            srt_path = Path(tempfile.gettempdir()) / f"{base_filename}.srt"
            srt_text = create_srt_from_json(result_data, max_chars=40, max_duration=4.0, polish=polish)
            async with aiofiles.open(srt_path, "w", encoding="utf-8") as f:
                await f.write(srt_text)
            temp_files_to_clean.append(srt_path)

            logging.info(f"[{file_info['id']}] 7a: Uploading .srt file to Slack.")
            await client.files_upload_v2(
                channel=channel_id,
                file=str(srt_path),
                title=f"{base_filename}.srt",
                initial_comment=f":heavy_check_mark: Все вийшло, ось субтитри для файлу `{file_info['name']}`.",
                thread_ts=thread_ts
            )

        if mode in ("txt_only", "both"):
            logging.info(f"[{file_info['id']}] 6b: Generating .txt transcript content.")
            transcript_content = create_transcript(result_data)

            # --- Google Drive Integration ---
            doc_link = None
            user_folder_link = None
            folder_created = False

            if DEBUG and not DEBUG_GDRIVE:
                logging.info(f"[{file_info['id']}] Skipping Google Drive upload (debug mode)")
            else:
                try:
                    logging.info(f"[{file_info['id']}] 7b-gdrive: Attempting Google Drive upload.")
                    drive_service = get_google_drive_service()

                    if drive_service:
                        user_info = await client.users_info(user=user_id)
                        username = user_info['user']['profile']['display_name'] or user_info['user']['name']
                        logging.info(f"[{file_info['id']}] Found user {user_id}")
                        shared_drive_id = get_or_create_shared_drive(drive_service)
                        if shared_drive_id:
                            user_folder_id, user_folder_link, created = find_or_create_folder(drive_service, username, parent_id=shared_drive_id)
                            if user_folder_id:
                                doc_base_filename = base_filename
                                if doc_base_filename.lower().endswith('.txt'):
                                    doc_base_filename = doc_base_filename[:-4]
                                if doc_base_filename.lower().endswith('.docx'):
                                    doc_base_filename = doc_base_filename[:-5]
                                doc_link = upload_as_google_doc(drive_service, doc_base_filename, transcript_content, user_folder_id)
                                if doc_link:
                                    if batch_context is not None:
                                        batch_context['gdrive_links'].append((file_info['name'], doc_link, user_folder_link))
                                    folder_created = created
                                    logging.info(f"[{file_info['id']}] 7b-gdrive: Successfully created doc.")
                                else:
                                    raise Exception("Failed to create text file.")
                            else:
                                raise Exception("Failed to find or create user folder.")
                        else:
                            raise Exception("Failed to find Transponster shared drive.")
                    else:
                        raise Exception("Failed to get Google Drive service.")
                except Exception as e:
                    logging.error(f"[{file_info['id']}] Google Drive integration failed: {e}. Continuing with Slack upload only.")

            # Upload .txt to Slack
            txt_path = Path(tempfile.gettempdir()) / f"{base_filename}.txt"
            async with aiofiles.open(txt_path, "w", encoding="utf-8") as f:
                await f.write(transcript_content)
            temp_files_to_clean.append(txt_path)

            logging.info(f"[{file_info['id']}] 7b: Uploading .txt file to Slack.")
            upload_result = await client.files_upload_v2(
                channel=channel_id,
                file=str(txt_path),
                title=f"{base_filename}.txt",
                initial_comment=f":heavy_check_mark: Все вийшло, ось розшифровка для файлу `{file_info['name']}`.",
                thread_ts=thread_ts
            )
            await asyncio.sleep(2)

            # Save mapping from Slack file ID to Drive file ID
            if doc_link:
                drive_file_id = _extract_drive_file_id(doc_link)
                # files_upload_v2 returns {'ok': True, 'file': {'id': ...}} or {'files': [...]}
                uploaded_files = upload_result.get('files') or ([upload_result.get('file')] if upload_result.get('file') else [])
                if drive_file_id and uploaded_files:
                    for uploaded_file in uploaded_files:
                        if uploaded_file and uploaded_file.get('id'):
                            save_file_mapping(uploaded_file['id'], drive_file_id)
                            break

            # Post Google Drive message (single file only; batch handled separately)
            if batch_context is None and doc_link and user_folder_link:
                if folder_created:
                    message = f"📂 Я створив для тебе <{user_folder_link}|папку> у нас на Google Drive і поклав розшифровку туди. <{doc_link}|Ось твоє посилання на файл>."
                else:
                    message = f"📂 Цю розшифровку ти також знайдеш в <{user_folder_link}|оцій папці> як Word документ. <{doc_link}|Ось твоє посилання на файл>."
                await client.chat_postMessage(channel=channel_id, text=message, thread_ts=thread_ts)

        logging.info(f"[{file_info['id']}] 8: Successfully uploaded results.")

    finally:
        for fpath in temp_files_to_clean:
            if fpath.exists():
                cleanup_temp_file(str(fpath))

async def process_single_file(file_id: str, user_id: str, channel_id: str, thread_ts: str, client, batch_context=None):
    logging.info(f"[{file_id}] Starting async processing for single file for user {user_id}.")
    file_info = {}

    try:
        logging.info(f"[{file_id}] 1: Retrieving file info.")
        file_info = (await client.files_info(file=file_id))["file"]

        if file_info.get('filetype') == 'quip':
            logging.info(f"[{file_id}] Ignoring Canvas file: {file_info.get('title')}")
            return

        if is_text_file(file_info.get("name", "")):
            logging.info(f"[{file_id}] Ignoring text/markdown file: {file_info['name']}")
            return

        logging.info(f"[{file_id}] 2: Checking if file is audio/video.")
        if not is_audio_or_video(file_info):
            extensions = "`" + "`, `".join(SUPPORTED_EXTENSIONS[:-1]) + "` або `" + SUPPORTED_EXTENSIONS[-1] + "`" if len(SUPPORTED_EXTENSIONS) > 1 else f"`{SUPPORTED_EXTENSIONS[0]}`"
            await client.chat_postMessage(channel=channel_id, text=f":no_good: Сорі, файл `{file_info['name']}` не аудіо і не відео. Таке я тобі не розшифрую. Будь ласка, дай мені файл у форматі {extensions}.", thread_ts=thread_ts)
            return

        logging.info(f"[{file_id}] 3: Checking if file is too large (>1000 MB).")
        if file_too_large(file_info):
            await client.chat_postMessage(channel=channel_id, text=f":no_good: Сорі, файл `{file_info['name']}` завеликий (>1000 МБ).", thread_ts=thread_ts)
            return

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            local_file_path = temp_dir_path / f"{file_info['id']}_{file_info['name']}"

            logging.info(f"[{file_id}] 4: Downloading file to {local_file_path}.")
            await download_file_streamed(file_info["url_private"], local_file_path, client.token)

            logging.info(f"[{file_id}] 5: Transcribing in background thread.")

            transcription_result = await transcribe_with_retries(
                local_file_path, file_info.get("name", file_id),
                user_id, channel_id, thread_ts, client,
            )

            if transcription_result is None:
                logging.error(f"[{file_id}] Transcription failed after all retries.")
                return

            logging.info(f"[{file_id}] 6: Processing transcription result.")
            filename_lower = file_info["name"].lower()
            mode = "txt_only"
            polish = is_korotulka_filename(file_info["name"])  # коротульки → editorial SRT rules
            if polish or "subtitles" in filename_lower or "субтитри" in filename_lower: mode = "srt_only"
            elif "both" in filename_lower or "обидва" in filename_lower: mode = "both"
            logging.info(f"[{file_id}] Determined transcription mode: {mode} (polish={polish})")

            base_filename = Path(file_info["name"]).stem

            # Resolve username for stats
            _username = await resolve_username(user_id, client)
            record_transcription(
                user_id=user_id,
                username=_username,
                channel_id=channel_id,
                filename=file_info.get("name", ""),
                mode=mode,
                file_size=file_info.get("size", 0),
            )

            logging.info(f"[{file_id}] 7: Generating and uploading results.")
            await generate_and_upload_results(mode, base_filename, transcription_result, file_info, user_id, channel_id, thread_ts, client, batch_context=batch_context, polish=polish)

    except Exception as e:
        logging.error(f"[{file_id}] An error occurred in async transcription flow: {e}", exc_info=True)
        file_name = file_info.get("name", file_id)
        try:
            await client.chat_postMessage(channel=channel_id, text=f":expressionless: Сорі, щось пішло не так з файлом `{file_name}`. Помилка: {e}", thread_ts=thread_ts)
        except Exception as slack_err:
            logging.error(f"[{file_id}] Failed to send error message to Slack: {slack_err}")
    finally:
        logging.info(f"[{file_id}] 9: Finished processing and cleanup.")

async def process_batch_async(batch_key: tuple, client):
    await asyncio.sleep(BATCH_WINDOW_SECONDS)

    async with batch_lock:
        if batch_key not in upload_batch_tasks:
            return
        batch_task_info = upload_batch_tasks.pop(batch_key)

    final_file_ids = list(dict.fromkeys(batch_task_info["file_ids"]))  # Deduplicate while preserving order
    thread_ts = batch_task_info["thread_ts"]
    user_id, channel_id = batch_key

    # Mark files as processed to prevent duplicate events from triggering reprocessing
    processed_file_ids.update(final_file_ids)

    processable_file_ids = []
    for file_id in final_file_ids:
        try:
            file_info = (await client.files_info(file=file_id))["file"]
            filename = file_info.get("name", "")
            if not is_text_file(filename):
                processable_file_ids.append(file_id)
            else:
                logging.info(f"[{file_id}] Ignoring text/markdown file in batch pre-check: {file_info['name']}")
        except Exception as e:
            logging.error(f"Could not get file_info for {file_id} during batch processing: {e}. Skipping.")

    if not processable_file_ids:
        logging.info(f"Batch for {batch_key} contained no processable files. Aborting.")
        return

    file_count = len(processable_file_ids)
    file_word = get_file_word(file_count)
    confirmation_text = f":saluting_face: Забираю в роботу {file_count} {file_word}. Відпишу тобі по кожному окремо, коли я буду готовий, або якщо поламаюся." if file_count > 1 else ":saluting_face: Забираю в роботу. Відпишу тобі, коли я буду готовий, або якщо поламаюся."

    await client.chat_postMessage(channel=channel_id, text=confirmation_text, thread_ts=thread_ts)
    logging.info(f"Processing batch of {file_count} files for {batch_key}")

    batch_context = {'gdrive_links': []} if file_count > 1 else None
    processing_tasks = [
        process_single_file(file_id, user_id, channel_id, thread_ts, client, batch_context=batch_context)
        for file_id in processable_file_ids
    ]
    await asyncio.gather(*processing_tasks)

    # After all files processed, if multi-file and any links, send combined message
    if batch_context and batch_context['gdrive_links']:
        user_folder_link = batch_context['gdrive_links'][0][2]  # All files in same folder
        lines = [f":open_file_folder: Ці розшифровки ти також знайдеш в <{user_folder_link}|оцій папці> як Word документи."]
        for filename, doc_link, _ in batch_context['gdrive_links']:
            lines.append(f"• <{doc_link}|Ось твоє посилання на файл> `{filename}`.")
        summary_message = "\n".join(lines)
        await client.chat_postMessage(channel=channel_id, text=summary_message, thread_ts=thread_ts)

@app.event("file_shared")
async def handle_file_shared_events(event, client):
    file_id = event["file_id"]
    user_id = event["user_id"]
    channel_id = event.get("channel_id")

    if not channel_id:
        logging.warning(f"File {file_id} shared without channel_id. Ignoring.")
        return

    # Skip files that have already been processed
    if file_id in processed_file_ids:
        logging.info(f"File {file_id} already processed. Skipping duplicate event.")
        return

    batch_key = (user_id, channel_id)

    async with batch_lock:
        if batch_key in upload_batch_tasks:
            upload_batch_tasks[batch_key]["file_ids"].append(file_id)
            logging.info(f"Adding file {file_id} to existing batch {batch_key}.")
            return
        else:
            logging.info(f"Starting new batch for {batch_key} with file {file_id}.")
            try:
                file_info = (await client.files_info(file=file_id))["file"]
                thread_ts = get_thread_ts(file_info, channel_id)
            except Exception as e:
                logging.error(f"Could not get file_info for {file_id}: {e}. Aborting.")
                return

            batch_info = {"file_ids": [file_id], "thread_ts": thread_ts}
            upload_batch_tasks[batch_key] = batch_info

            asyncio.create_task(process_batch_async(batch_key, client))
            logging.info(f"Timer started for batch {batch_key}. Will process in {BATCH_WINDOW_SECONDS}s.")


# --- Translation, Cleanup and Re-transcription via Emoji Reactions ---
ENGLISH_FLAG_EMOJIS = {"flag-gb", "flag-us", "flag-england", "gb", "us", "uk"}
CLEANUP_EMOJIS = {"broom"}
UKRAINIAN_FLAG_EMOJIS = {"flag-ua", "ua"}
ALL_PROCESSING_EMOJIS = ENGLISH_FLAG_EMOJIS | CLEANUP_EMOJIS | UKRAINIAN_FLAG_EMOJIS
processed_reaction_requests = set()

UKRAINIAN_LANGUAGE_CODE = "ukr"
# Suffixes the bot itself appends. Stripped when matching a transcript back to
# the media it came from, so a reaction on `Interview-clean.txt` still finds
# `Interview.m4a`.
DERIVED_SUFFIXES = ("-clean", "-eng", "-ukr")


def _forget_reaction_request(request_key: str):
    """Drop the dedup key so the user can retry by re-adding the reaction."""
    processed_reaction_requests.discard(request_key)


def strip_derived_suffixes(stem: str) -> str:
    """`Interview-clean-eng` → `Interview`. Suffixes can stack, so loop."""
    changed = True
    while changed:
        changed = False
        for suffix in DERIVED_SUFFIXES:
            if stem.lower().endswith(suffix):
                stem = stem[: -len(suffix)]
                changed = True
    return stem


@app.event("reaction_added")
async def handle_reaction_added(event, client):
    """Handle emoji reactions for translation and cleanup requests."""
    reaction = event.get("reaction", "")
    user_id = event.get("user")
    item = event.get("item", {})

    logging.info(f"[reaction] Received reaction: '{reaction}' from user {user_id}")

    # Only process known emojis
    if reaction not in ALL_PROCESSING_EMOJIS:
        logging.info(f"[reaction] Ignoring reaction '{reaction}' - not in {ALL_PROCESSING_EMOJIS}")
        return

    # Only process reactions on messages
    if item.get("type") != "message":
        return

    channel_id = item.get("channel")
    message_ts = item.get("ts")

    if not channel_id or not message_ts:
        return

    # Create a unique key for this request
    request_key = f"{channel_id}:{message_ts}:{reaction}"
    if request_key in processed_reaction_requests:
        logging.info(f"Reaction request {request_key} already processed. Skipping.")
        return
    processed_reaction_requests.add(request_key)

    is_cleanup = reaction in CLEANUP_EMOJIS
    is_retranscribe = reaction in UKRAINIAN_FLAG_EMOJIS
    action_name = "cleanup" if is_cleanup else ("retranscribe" if is_retranscribe else "translation")
    logging.info(f"[{action_name}] Received {reaction} emoji on message {message_ts} in {channel_id}")

    try:
        # Check if the bot is in this channel before doing anything
        try:
            result = await client.conversations_history(
                channel=channel_id,
                latest=message_ts,
                limit=1,
                inclusive=True
            )
        except Exception as e:
            if "not_in_channel" in str(e):
                logging.info(f"[translation] Bot not in channel {channel_id}, ignoring reaction")
                return
            raise

        messages = result.get("messages", [])
        message = messages[0] if messages else None

        # Check if this message is a thread reply - if so, we need conversations_replies
        if message and message.get("thread_ts") and message.get("thread_ts") != message_ts:
            thread_ts = message.get("thread_ts")
            replies_result = await client.conversations_replies(
                channel=channel_id,
                ts=thread_ts
            )
            for reply in replies_result.get("messages", []):
                if reply.get("ts") == message_ts:
                    message = reply
                    break

        # If conversations_history returned the parent but we reacted to a reply,
        # the ts won't match - try conversations_replies with the message_ts as thread
        if not message or message.get("ts") != message_ts:
            try:
                replies_result = await client.conversations_replies(
                    channel=channel_id,
                    ts=message_ts
                )
                for reply in replies_result.get("messages", []):
                    if reply.get("ts") == message_ts:
                        message = reply
                        break
            except Exception:
                pass

        if not message:
            logging.warning(f"[translation] Could not find message {message_ts}")
            return

        thread_ts = message.get("thread_ts") or message_ts

        logging.info(f"[{action_name}] Message ts: {message.get('ts')}, looking for: {message_ts}")
        logging.info(f"[{action_name}] Message files: {[f.get('name') for f in message.get('files', [])]}")

        # Re-transcription is allowed both on a transcript inside a thread and
        # directly on the original media message, so it runs before the
        # thread-only guard the other two actions need.
        if is_retranscribe:
            await handle_ukrainian_retranscription(
                message, message_ts, thread_ts, channel_id, user_id, client, request_key
            )
            return

        # Check if this message is in a thread (not the parent message)
        if not thread_ts or thread_ts == message_ts:
            logging.info(f"[{action_name}] Message {message_ts} is not in a thread. Ignoring.")
            action_word = "вичищення" if is_cleanup else "переклад"
            await client.chat_postMessage(
                channel=channel_id,
                text=f":no_good: Сорі, {action_word} працює лише в треді. Постав емоджі на повідомлення з файлом всередині треду.",
                thread_ts=message_ts
            )
            return

        # Look for processable files in the message
        files = message.get("files", [])
        srt_files = [f for f in files if f.get("name", "").lower().endswith(".srt")]
        txt_files = [f for f in files if f.get("name", "").lower().endswith(".txt")]

        if is_cleanup:
            # Cleanup only works on .txt files
            if txt_files:
                for txt_file in txt_files:
                    await process_txt_cleanup(txt_file, channel_id, thread_ts, client)
            else:
                logging.info(f"[cleanup] No .txt files found in message {message_ts}")
                await client.chat_postMessage(
                    channel=channel_id,
                    text=":no_good: Сорі, вичищення працює лише з `.txt` файлами розшифровок.",
                    thread_ts=thread_ts
                )
                return
        else:
            # Translation works on both .srt and .txt
            if srt_files:
                for srt_file in srt_files:
                    await process_srt_translation(srt_file, channel_id, thread_ts, client)
            elif txt_files:
                for txt_file in txt_files:
                    await process_txt_translation(txt_file, channel_id, thread_ts, client)
            else:
                logging.info(f"[translation] No .srt or .txt files found in message {message_ts}")
                await client.chat_postMessage(
                    channel=channel_id,
                    text=":no_good: Сорі, це не файл для перекладу. Мені потрібен файл з розширенням `.srt` або `.txt`.",
                    thread_ts=thread_ts
                )
                return

    except Exception as e:
        logging.error(f"[{action_name}] Error handling reaction: {e}", exc_info=True)
        # An unexpected crash shouldn't burn the request key — let the user
        # retry by removing and re-adding the emoji.
        _forget_reaction_request(request_key)


async def process_srt_translation(file_info: dict, channel_id: str, thread_ts: str, client):
    """Download, translate, and upload an SRT file."""
    file_id = file_info.get("id")
    file_name = file_info.get("name", "subtitles.srt")
    base_name = Path(file_name).stem

    logging.info(f"[translation:{file_id}] Starting translation for {file_name}")

    # Send confirmation message
    await client.chat_postMessage(
        channel=channel_id,
        text=f":saluting_face: Дістаю словничок і йду перекладать субтитри для файлу `{file_name}`. Відпишу тобі, коли буду готовий.",
        thread_ts=thread_ts
    )

    try:
        # Get fresh file info with download URL
        fresh_file_info = (await client.files_info(file=file_id))["file"]
        url_private = fresh_file_info.get("url_private")

        if not url_private:
            raise ValueError("Could not get file download URL")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            local_path = temp_dir_path / file_name

            logging.info(f"[translation:{file_id}] Downloading file")
            await download_file_streamed(url_private, local_path, client.token)

            async with aiofiles.open(local_path, "r", encoding="utf-8") as f:
                srt_content = await f.read()

            logging.info(f"[translation:{file_id}] Parsing SRT content")
            entries = parse_srt_content(srt_content)

            if not entries:
                raise ValueError("Could not parse SRT file - no valid entries found")

            texts = [entry["text"] for entry in entries]
            logging.info(f"[translation:{file_id}] Translating {len(texts)} subtitle entries")

            translations = await translate_texts_with_openai(texts)

            logging.info(f"[translation:{file_id}] Rebuilding SRT with translations")
            translated_srt = rebuild_srt_with_translations(entries, translations)

            translated_filename = f"{base_name}-eng.srt"
            translated_path = temp_dir_path / translated_filename

            async with aiofiles.open(translated_path, "w", encoding="utf-8") as f:
                await f.write(translated_srt)

            logging.info(f"[translation:{file_id}] Uploading translated file")
            await client.files_upload_v2(
                channel=channel_id,
                file=str(translated_path),
                title=translated_filename,
                initial_comment=f":heavy_check_mark: Все вийшло, ось переклад субтитрів для файлу `{file_name}`.",
                thread_ts=thread_ts
            )

            record_transcription(
                user_id="unknown",
                username="",
                channel_id=channel_id,
                filename=file_name,
                mode="translation",
            )
            logging.info(f"[translation:{file_id}] Translation complete")

    except Exception as e:
        logging.error(f"[translation:{file_id}] Error: {e}", exc_info=True)
        await client.chat_postMessage(
            channel=channel_id,
            text=f":expressionless: Сорі, мені не вдалося перекласти `{file_name}`. Помилка: {e}",
            thread_ts=thread_ts
        )


async def process_txt_translation(file_info: dict, channel_id: str, thread_ts: str, client):
    """Download, translate, and upload a .txt transcript file."""
    file_id = file_info.get("id")
    file_name = file_info.get("name", "transcript.txt")
    base_name = Path(file_name).stem

    logging.info(f"[txt-translation:{file_id}] Starting translation for {file_name}")

    # Send confirmation message
    await client.chat_postMessage(
        channel=channel_id,
        text=f":saluting_face: Дістаю словничок і йду перекладать файл `{file_name}`. Відпишу тобі, коли буду готовий.",
        thread_ts=thread_ts
    )

    try:
        # Get fresh file info with download URL
        fresh_file_info = (await client.files_info(file=file_id))["file"]
        url_private = fresh_file_info.get("url_private")

        if not url_private:
            raise ValueError("Could not get file download URL")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            local_path = temp_dir_path / file_name

            logging.info(f"[txt-translation:{file_id}] Downloading file")
            await download_file_streamed(url_private, local_path, client.token)

            async with aiofiles.open(local_path, "r", encoding="utf-8") as f:
                txt_content = await f.read()

            logging.info(f"[txt-translation:{file_id}] Parsing transcript content")
            entries = parse_transcript_content(txt_content)

            if not entries:
                await client.chat_postMessage(
                    channel=channel_id,
                    text=f":no_good: Сорі, мені не вдалося зчитати файл `{file_name}`. Це не схоже на мою розшифровку. Я лиш в свої умію.",
                    thread_ts=thread_ts
                )
                return

            texts = [entry["text"] for entry in entries]
            logging.info(f"[txt-translation:{file_id}] Translating {len(texts)} transcript entries")
            translations = await translate_texts_with_openai(texts)

            logging.info(f"[txt-translation:{file_id}] Rebuilding transcript with translations")
            translated_txt = rebuild_transcript_with_translations(entries, translations)

            translated_filename = f"{base_name}-eng.txt"
            translated_path = temp_dir_path / translated_filename

            async with aiofiles.open(translated_path, "w", encoding="utf-8") as f:
                await f.write(translated_txt)

            logging.info(f"[txt-translation:{file_id}] Uploading translated file")
            await client.files_upload_v2(
                channel=channel_id,
                file=str(translated_path),
                title=translated_filename,
                initial_comment=f":heavy_check_mark: Все вийшло, ось переклад розшифровки для файлу `{file_name}`.",
                thread_ts=thread_ts
            )

            # Try to update the corresponding Google Drive document
            drive_file_id = get_drive_file_id(file_id)
            if drive_file_id:
                logging.info(f"[txt-translation:{file_id}] Found Drive mapping: {drive_file_id}")
                try:
                    drive_service = get_google_drive_service()
                    if drive_service:
                        doc_link = update_docx_with_translation(drive_service, drive_file_id, translated_txt)
                        if doc_link:
                            await client.chat_postMessage(
                                channel=channel_id,
                                text=f":open_file_folder: Я також поклав переклад в оригінальний <{doc_link}|документ на Google Drive>.",
                                thread_ts=thread_ts
                            )
                            logging.info(f"[txt-translation:{file_id}] Updated Drive doc successfully")
                        else:
                            logging.warning(f"[txt-translation:{file_id}] Failed to update Drive doc")
                except Exception as e:
                    logging.error(f"[txt-translation:{file_id}] Error updating Drive doc: {e}")
            else:
                logging.info(f"[txt-translation:{file_id}] No Drive mapping found")
                await client.chat_postMessage(
                    channel=channel_id,
                    text=":card_index: Сорі, я не знайшов документ на Google Drive для цієї розшифровки, тому переклад поклав лише у тред.",
                    thread_ts=thread_ts
                )

            record_transcription(
                user_id="unknown",
                username="",
                channel_id=channel_id,
                filename=file_name,
                mode="translation",
            )
            logging.info(f"[txt-translation:{file_id}] Translation complete")

    except Exception as e:
        logging.error(f"[txt-translation:{file_id}] Error: {e}", exc_info=True)
        await client.chat_postMessage(
            channel=channel_id,
            text=f":expressionless: Сорі, мені не вдалося перекласти `{file_name}`. Помилка: {e}",
            thread_ts=thread_ts
        )


async def process_txt_cleanup(file_info: dict, channel_id: str, thread_ts: str, client):
    """Download, clean up filler words, and upload a .txt transcript file."""
    file_id = file_info.get("id")
    file_name = file_info.get("name", "transcript.txt")
    base_name = Path(file_name).stem

    logging.info(f"[cleanup:{file_id}] Starting cleanup for {file_name}")

    await client.chat_postMessage(
        channel=channel_id,
        text=f":broom: Пішов мести мітлою файл `{file_name}`. Відпишу тобі, коли буду готовий.",
        thread_ts=thread_ts
    )

    try:
        fresh_file_info = (await client.files_info(file=file_id))["file"]
        url_private = fresh_file_info.get("url_private")

        if not url_private:
            raise ValueError("Could not get file download URL")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            local_path = temp_dir_path / file_name

            logging.info(f"[cleanup:{file_id}] Downloading file")
            await download_file_streamed(url_private, local_path, client.token)

            async with aiofiles.open(local_path, "r", encoding="utf-8") as f:
                txt_content = await f.read()

            logging.info(f"[cleanup:{file_id}] Parsing transcript content")
            entries = parse_transcript_content(txt_content)

            if not entries:
                await client.chat_postMessage(
                    channel=channel_id,
                    text=f":no_good: Сорі, не вдалося зчитати файл `{file_name}`. Це не схоже на мою розшифровку. Я лиш в свої умію.",
                    thread_ts=thread_ts
                )
                return

            texts = [entry["text"] for entry in entries]
            logging.info(f"[cleanup:{file_id}] Cleaning {len(texts)} transcript entries")
            cleaned_texts = await clean_texts_with_openai(texts)

            logging.info(f"[cleanup:{file_id}] Rebuilding transcript with cleaned text")
            cleaned_txt = rebuild_transcript_with_translations(entries, cleaned_texts)

            cleaned_filename = f"{base_name}-clean.txt"
            cleaned_path = temp_dir_path / cleaned_filename

            async with aiofiles.open(cleaned_path, "w", encoding="utf-8") as f:
                await f.write(cleaned_txt)

            logging.info(f"[cleanup:{file_id}] Uploading cleaned file")
            await client.files_upload_v2(
                channel=channel_id,
                file=str(cleaned_path),
                title=cleaned_filename,
                initial_comment=f":sparkles: Все вийшло, ось вичищена розшифровка для файлу `{file_name}`.",
                thread_ts=thread_ts
            )

            # Try to update the corresponding Google Drive document
            drive_file_id = get_drive_file_id(file_id)
            if drive_file_id:
                logging.info(f"[cleanup:{file_id}] Found Drive mapping: {drive_file_id}")
                try:
                    drive_service = get_google_drive_service()
                    if drive_service:
                        doc_link = update_docx_with_cleanup(drive_service, drive_file_id, cleaned_txt)
                        if doc_link:
                            await client.chat_postMessage(
                                channel=channel_id,
                                text=f":open_file_folder: Я також поклав почищену версію в оригінальний <{doc_link}|документ на Google Drive>.",
                                thread_ts=thread_ts
                            )
                            logging.info(f"[cleanup:{file_id}] Updated Drive doc successfully")
                        else:
                            logging.warning(f"[cleanup:{file_id}] Failed to update Drive doc")
                except Exception as e:
                    logging.error(f"[cleanup:{file_id}] Error updating Drive doc: {e}")
            else:
                logging.info(f"[cleanup:{file_id}] No Drive mapping found")
                await client.chat_postMessage(
                    channel=channel_id,
                    text=":card_index: Сорі, я не знайшов документ на Google Drive для цієї розшифровки, тому почищену версію поклав лише у тред.",
                    thread_ts=thread_ts
                )

            record_transcription(
                user_id="unknown",
                username="",
                channel_id=channel_id,
                filename=file_name,
                mode="cleanup",
            )
            logging.info(f"[cleanup:{file_id}] Cleanup complete")

    except Exception as e:
        logging.error(f"[cleanup:{file_id}] Error: {e}", exc_info=True)
        await client.chat_postMessage(
            channel=channel_id,
            text=f":pensive: Сорі, не вдалося вичистити `{file_name}`. Помилка: {e}",
            thread_ts=thread_ts
        )


# --- Re-transcription with Ukrainian forced (🇺🇦 reaction) ---

async def fetch_thread_messages(channel_id: str, thread_ts: str, client) -> list:
    """Every message in the thread, oldest first."""
    result = await client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200)
    return result.get("messages", [])


def find_source_media(messages: list, target_stem: str | None):
    """
    Find the audio/video a transcript came from.

    Scans the whole thread rather than just the parent, because the media may
    itself have been posted as a reply inside someone else's thread.

    Returns (file_info, reason) — reason is set only when file_info is None.
    """
    candidates = [
        f for message in messages
        for f in message.get("files", [])
        if is_audio_or_video(f)
    ]

    if not candidates:
        return None, "no_media"

    if target_stem:
        for f in candidates:
            if Path(f.get("name", "")).stem.lower() == target_stem.lower():
                return f, None

    # A single media file in the thread is unambiguous even if the name drifted.
    if len(candidates) == 1:
        return candidates[0], None

    return None, "ambiguous"


def find_drive_file_id(messages: list, target_stem: str | None, preferred_file_id: str | None) -> str | None:
    """
    Locate the Drive doc for this transcript. Mappings are keyed by the Slack
    file ID of the .txt, so when the reaction landed on the media instead we
    fall back to scanning the thread for a .txt that has one.
    """
    if preferred_file_id:
        drive_file_id = get_drive_file_id(preferred_file_id)
        if drive_file_id:
            return drive_file_id

    for message in messages:
        for f in message.get("files", []):
            name = f.get("name", "")
            if not name.lower().endswith(".txt"):
                continue
            if target_stem and strip_derived_suffixes(Path(name).stem).lower() != target_stem.lower():
                continue
            drive_file_id = get_drive_file_id(f.get("id"))
            if drive_file_id:
                return drive_file_id
    return None


async def handle_ukrainian_retranscription(message: dict, message_ts: str, thread_ts: str,
                                           channel_id: str, user_id: str, client, request_key: str):
    """
    Work out what to re-transcribe from where the 🇺🇦 landed, then hand off.

    Two valid targets: a transcript the bot posted (walk the thread back to its
    source media) or the media message itself (use it directly).
    """
    files = message.get("files", [])
    media_files = [f for f in files if is_audio_or_video(f)]
    srt_files = [f for f in files if f.get("name", "").lower().endswith(".srt")]
    txt_files = [f for f in files if f.get("name", "").lower().endswith(".txt")]

    # Reacted straight on the media: no lookup needed, redo every file in it.
    if media_files:
        messages = await fetch_thread_messages(channel_id, thread_ts, client)
        for media_file in media_files:
            filename_lower = media_file.get("name", "").lower()
            polish = is_korotulka_filename(media_file.get("name", ""))
            if polish or "subtitles" in filename_lower or "субтитри" in filename_lower:
                mode = "srt_only"
            elif "both" in filename_lower or "обидва" in filename_lower:
                mode = "both"
            else:
                mode = "txt_only"
            target_stem = Path(media_file.get("name", "")).stem
            drive_file_id = find_drive_file_id(messages, target_stem, None)
            await process_ukrainian_retranscription(
                media_file, mode, polish, drive_file_id,
                channel_id, thread_ts, user_id, client, request_key,
            )
        return

    reacted_file = (srt_files or txt_files or [None])[0]
    if not reacted_file:
        await client.chat_postMessage(
            channel=channel_id,
            text=":no_good: Сорі, це не той файл. Постав :flag-ua: на розшифровку (`.txt` або `.srt`) чи на саме аудіо або відео.",
            thread_ts=thread_ts
        )
        _forget_reaction_request(request_key)
        return

    reacted_name = reacted_file.get("name", "")
    target_stem = strip_derived_suffixes(Path(reacted_name).stem)
    mode = "srt_only" if reacted_name.lower().endswith(".srt") else "txt_only"

    if thread_ts == message_ts:
        logging.info(f"[retranscribe] {reacted_name} is not in a thread, no source media to find.")
        await client.chat_postMessage(
            channel=channel_id,
            text=":no_good: Сорі, не бачу тут вихідного аудіо. Постав :flag-ua: на мою розшифровку всередині треду або прямо на аудіофайл.",
            thread_ts=message_ts
        )
        _forget_reaction_request(request_key)
        return

    messages = await fetch_thread_messages(channel_id, thread_ts, client)
    source_media, reason = find_source_media(messages, target_stem)

    if not source_media:
        logging.info(f"[retranscribe] No usable source media for {reacted_name}: {reason}")
        if reason == "ambiguous":
            text = (":thinking_face: У цьому треді кілька медіафайлів, і я не зрозумів, який із них твій. "
                    "Постав :flag-ua: прямо на повідомлення з потрібним аудіо, і я візьмуся.")
        else:
            text = (":no_good: Сорі, вихідного аудіо в цьому треді вже немає — без нього перерозшифрувати не вийде. "
                    "Закинь файл ще раз, і я зроблю все з нуля.")
        await client.chat_postMessage(channel=channel_id, text=text, thread_ts=thread_ts)
        _forget_reaction_request(request_key)
        return

    polish = is_korotulka_filename(source_media.get("name", ""))
    drive_file_id = find_drive_file_id(messages, target_stem, reacted_file.get("id"))

    await process_ukrainian_retranscription(
        source_media, mode, polish, drive_file_id,
        channel_id, thread_ts, user_id, client, request_key,
    )


async def process_ukrainian_retranscription(source_media: dict, mode: str, polish: bool,
                                            drive_file_id: str | None, channel_id: str,
                                            thread_ts: str, user_id: str, client, request_key: str):
    """Download the original media and transcribe it again with Ukrainian forced."""
    file_id = source_media.get("id")
    file_name = source_media.get("name", "audio")
    base_filename = Path(file_name).stem

    logging.info(f"[retranscribe:{file_id}] Starting Ukrainian re-transcription for {file_name}")

    await client.chat_postMessage(
        channel=channel_id,
        text=f":saluting_face: Беру `{file_name}` на другий захід — цього разу українську задам примусово. "
             f"Відпишу, як буде готово, або якщо щось піде не так.",
        thread_ts=thread_ts
    )

    try:
        fresh_file_info = (await client.files_info(file=file_id))["file"]

        if not is_audio_or_video(fresh_file_info):
            await client.chat_postMessage(
                channel=channel_id,
                text=f":no_good: Сорі, `{file_name}` не аудіо і не відео. Таке я не розшифрую.",
                thread_ts=thread_ts
            )
            _forget_reaction_request(request_key)
            return

        if file_too_large(fresh_file_info):
            await client.chat_postMessage(
                channel=channel_id,
                text=f":no_good: Сорі, файл `{file_name}` завеликий (>1000 МБ).",
                thread_ts=thread_ts
            )
            _forget_reaction_request(request_key)
            return

        url_private = fresh_file_info.get("url_private")
        if not url_private:
            raise ValueError("Could not get file download URL")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            local_path = temp_dir_path / f"{file_id}_{file_name}"

            logging.info(f"[retranscribe:{file_id}] Downloading source media")
            await download_file_streamed(url_private, local_path, client.token)

            logging.info(f"[retranscribe:{file_id}] Transcribing with language_code={UKRAINIAN_LANGUAGE_CODE}")
            result_data = await transcribe_with_retries(
                local_path, file_name, user_id, channel_id, thread_ts, client,
                language_code=UKRAINIAN_LANGUAGE_CODE,
            )

            if result_data is None:
                logging.error(f"[retranscribe:{file_id}] Transcription failed after all retries.")
                _forget_reaction_request(request_key)
                return

            username = await resolve_username(user_id, client)

            if mode in ("srt_only", "both"):
                srt_filename = f"{base_filename}-ukr.srt"
                srt_path = temp_dir_path / srt_filename
                srt_text = create_srt_from_json(result_data, max_chars=40, max_duration=4.0, polish=polish)
                async with aiofiles.open(srt_path, "w", encoding="utf-8") as f:
                    await f.write(srt_text)

                logging.info(f"[retranscribe:{file_id}] Uploading {srt_filename}")
                await client.files_upload_v2(
                    channel=channel_id,
                    file=str(srt_path),
                    title=srt_filename,
                    initial_comment=f":heavy_check_mark: Все вийшло, ось нові субтитри для файлу `{file_name}` "
                                    f"— цього разу з примусовою українською.",
                    thread_ts=thread_ts
                )

            if mode in ("txt_only", "both"):
                transcript_content = create_transcript(result_data)
                txt_filename = f"{base_filename}-ukr.txt"
                txt_path = temp_dir_path / txt_filename
                async with aiofiles.open(txt_path, "w", encoding="utf-8") as f:
                    await f.write(transcript_content)

                logging.info(f"[retranscribe:{file_id}] Uploading {txt_filename}")
                upload_result = await client.files_upload_v2(
                    channel=channel_id,
                    file=str(txt_path),
                    title=txt_filename,
                    initial_comment=f":heavy_check_mark: Все вийшло, ось нова розшифровка для файлу `{file_name}` "
                                    f"— цього разу з примусовою українською.",
                    thread_ts=thread_ts
                )
                await asyncio.sleep(2)

                if drive_file_id:
                    logging.info(f"[retranscribe:{file_id}] Found Drive mapping: {drive_file_id}")
                    try:
                        drive_service = get_google_drive_service()
                        if drive_service:
                            doc_link = update_docx_with_ukrainian(drive_service, drive_file_id, transcript_content)
                            if doc_link:
                                await client.chat_postMessage(
                                    channel=channel_id,
                                    text=f":open_file_folder: Я також поклав цю версію в оригінальний "
                                         f"<{doc_link}|документ на Google Drive>.",
                                    thread_ts=thread_ts
                                )
                                logging.info(f"[retranscribe:{file_id}] Updated Drive doc successfully")
                            else:
                                logging.warning(f"[retranscribe:{file_id}] Failed to update Drive doc")
                    except Exception as e:
                        logging.error(f"[retranscribe:{file_id}] Error updating Drive doc: {e}")

                    # Carry the mapping over so 🧹 and 🇬🇧 keep working on the new file.
                    uploaded_files = upload_result.get('files') or (
                        [upload_result.get('file')] if upload_result.get('file') else []
                    )
                    for uploaded_file in uploaded_files:
                        if uploaded_file and uploaded_file.get('id'):
                            save_file_mapping(uploaded_file['id'], drive_file_id)
                            break
                else:
                    logging.info(f"[retranscribe:{file_id}] No Drive mapping found")
                    await client.chat_postMessage(
                        channel=channel_id,
                        text=":card_index: Сорі, я не знайшов документ на Google Drive для цієї розшифровки, "
                             "тому нову версію поклав лише у тред.",
                        thread_ts=thread_ts
                    )

            record_transcription(
                user_id=user_id,
                username=username,
                channel_id=channel_id,
                filename=file_name,
                mode="retranscribe_uk",
                file_size=fresh_file_info.get("size", 0),
            )
            logging.info(f"[retranscribe:{file_id}] Re-transcription complete")

    except Exception as e:
        logging.error(f"[retranscribe:{file_id}] Error: {e}", exc_info=True)
        _forget_reaction_request(request_key)
        await client.chat_postMessage(
            channel=channel_id,
            text=f":pensive: Сорі, не вдалося перерозшифрувати `{file_name}`. Помилка: {e}",
            thread_ts=thread_ts
        )
