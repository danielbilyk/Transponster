import os
import re
import asyncio
import logging
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import requests

import aiohttp
import aiofiles
from slack_bolt.async_app import AsyncApp

from config import SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, DEBUG, DEBUG_GDRIVE
from helpers import (
    is_audio_or_video, file_too_large, SUPPORTED_EXTENSIONS,
    transcribe_file, get_thread_ts, cleanup_temp_file,
    create_srt_from_json, create_transcript,
    get_google_drive_service, find_or_create_folder, upload_as_google_doc,
    get_or_create_shared_drive,
    parse_srt_content, translate_texts_with_openai, rebuild_srt_with_translations,
    parse_transcript_content, rebuild_transcript_with_translations,
    update_docx_with_translation
)

# --- Async Setup ---
blocking_task_executor = ThreadPoolExecutor(max_workers=os.cpu_count() or 4)
app = AsyncApp(
    token=SLACK_BOT_TOKEN,
    signing_secret=SLACK_SIGNING_SECRET
)
aiohttp_session = None
batch_lock = asyncio.Lock()
upload_batch_tasks = {}
processed_file_ids = set()  # Track files that have been processed to prevent duplicates
BATCH_WINDOW_SECONDS = 3.0
MAX_TRANSCRIPTION_RETRIES = 2
RETRY_DELAY_SECONDS = 3

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

async def run_transcription(local_path: Path) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(blocking_task_executor, transcribe_file, str(local_path))

async def generate_and_upload_results(mode: str, base_filename: str, result_data: dict, file_info: dict, user_id: str, channel_id: str, thread_ts: str, client, batch_context=None):
    upload_tasks = []
    temp_files_to_clean = []

    try:
        srt_upload_task = None
        if mode in ("srt_only", "both"):
            logging.info(f"[{file_info['id']}] 6a: Generating .srt file.")
            srt_path = Path(tempfile.gettempdir()) / f"{base_filename}.srt"
            srt_text = create_srt_from_json(result_data, max_chars=40, max_duration=4.0)
            async with aiofiles.open(srt_path, "w", encoding="utf-8") as f:
                await f.write(srt_text)
            temp_files_to_clean.append(srt_path)
            
            logging.info(f"[{file_info['id']}] 7a: Uploading .srt file to Slack.")
            srt_upload_task = client.files_upload_v2(
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
            drive_message_task = None
            doc_link = None
            user_folder_link = None

            # Skip Google Drive in debug mode unless explicitly enabled
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
                                # Remove .txt or .docx from base_filename for Google Drive doc
                                doc_base_filename = base_filename
                                if doc_base_filename.lower().endswith('.txt'):
                                    doc_base_filename = doc_base_filename[:-4]
                                if doc_base_filename.lower().endswith('.docx'):
                                    doc_base_filename = doc_base_filename[:-5]
                                doc_link = upload_as_google_doc(drive_service, doc_base_filename, transcript_content, user_folder_id)
                                if doc_link:
                                    if batch_context is not None:
                                        batch_context['gdrive_links'].append((file_info['name'], doc_link, user_folder_link))
                                    else:
                                        if created:
                                            message = f"📂 Я створив для тебе <{user_folder_link}|папку> у нас на Google Drive і поклав розшифровку туди. <{doc_link}|Ось твоє посилання на файл>."
                                        else:
                                            message = f"📂 Цю розшифровку ти також знайдеш в <{user_folder_link}|оцій папці> як Word документ. <{doc_link}|Ось твоє посилання на файл>."
                                    drive_message_task = client.chat_postMessage(channel=channel_id, text=message, thread_ts=thread_ts)
                                    logging.info(f"[{file_info['id']}] 7b-gdrive: Successfully created text file and prepared Slack message.")
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
            
            txt_path = Path(tempfile.gettempdir()) / f"{base_filename}.txt"
            async with aiofiles.open(txt_path, "w", encoding="utf-8") as f:
                await f.write(transcript_content)
            temp_files_to_clean.append(txt_path)

            logging.info(f"[{file_info['id']}] 7b: Uploading .txt file to Slack.")
            txt_upload_task = client.files_upload_v2(
                channel=channel_id,
                file=str(txt_path),
                title=f"{base_filename}.txt",
                initial_comment=f":heavy_check_mark: Все вийшло, ось розшифровка для файлу `{file_info['name']}`.",
                thread_ts=thread_ts
            )
            await txt_upload_task
            await asyncio.sleep(2)
            if drive_message_task:
                await drive_message_task

        if srt_upload_task:
            await srt_upload_task
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
            
            transcription_result = None
            for attempt in range(MAX_TRANSCRIPTION_RETRIES):
                try:
                    transcription_result = await run_transcription(local_file_path)
                    break  # Success
                except requests.exceptions.HTTPError as e:
                    if e.response is not None and 500 <= e.response.status_code < 600:
                        file_name = file_info.get("name", file_id)
                        if attempt < MAX_TRANSCRIPTION_RETRIES - 1:
                            logging.warning(f"Attempt {attempt + 1} failed for {file_name}: {e}. Retrying...")
                            error_message = (
                                f"😑 Сорі, щось пішло не так з файлом `{file_name}`. Помилка:\n\n"
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
                            return
                    else:
                        raise  # Re-raise other HTTP errors to be caught by the generic handler
            
            if transcription_result is None:
                logging.error(f"[{file_id}] Transcription failed after all retries, but no result was produced.")
                return

            logging.info(f"[{file_id}] 6: Processing transcription result.")
            filename_lower = file_info["name"].lower()
            mode = "txt_only"
            if "subtitles" in filename_lower or "субтитри" in filename_lower: mode = "srt_only"
            elif "both" in filename_lower or "обидва" in filename_lower: mode = "both"
            logging.info(f"[{file_id}] Determined transcription mode: {mode}")
            
            base_filename = Path(file_info["name"]).stem
            logging.info(f"[{file_id}] 7: Generating and uploading results.")
            await generate_and_upload_results(mode, base_filename, transcription_result, file_info, user_id, channel_id, thread_ts, client, batch_context=batch_context)

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

    # After all files processed, if multi-file and any links, send summary message
    if batch_context and batch_context['gdrive_links']:
        user_folder_link = batch_context['gdrive_links'][0][2]  # All files in same folder
        lines = [":open_file_folder: Ці розшифровки ти також знайдеш в <{}|оцій папці> як Word документи.".format(user_folder_link)]
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


# --- Translation via Emoji Reactions ---
ENGLISH_FLAG_EMOJIS = {"flag-gb", "flag-us", "flag-england", "gb", "us", "uk"}
processed_translation_requests = set()  # Track processed translations to prevent duplicates


@app.event("reaction_added")
async def handle_reaction_added(event, client):
    """Handle emoji reactions for translation requests."""
    reaction = event.get("reaction", "")
    user_id = event.get("user")
    item = event.get("item", {})

    logging.info(f"[reaction] Received reaction: '{reaction}' from user {user_id}")

    # Only process English flag emojis
    if reaction not in ENGLISH_FLAG_EMOJIS:
        logging.info(f"[reaction] Ignoring reaction '{reaction}' - not in {ENGLISH_FLAG_EMOJIS}")
        return

    # Only process reactions on messages
    if item.get("type") != "message":
        return

    channel_id = item.get("channel")
    message_ts = item.get("ts")

    if not channel_id or not message_ts:
        return

    # Create a unique key for this translation request
    translation_key = f"{channel_id}:{message_ts}:{reaction}"
    if translation_key in processed_translation_requests:
        logging.info(f"Translation request {translation_key} already processed. Skipping.")
        return
    processed_translation_requests.add(translation_key)

    logging.info(f"[translation] Received {reaction} emoji on message {message_ts} in {channel_id}")

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
            # This is a reply in a thread, fetch via conversations_replies
            thread_ts = message.get("thread_ts")
            replies_result = await client.conversations_replies(
                channel=channel_id,
                ts=thread_ts
            )
            # Find the specific message by ts
            for reply in replies_result.get("messages", []):
                if reply.get("ts") == message_ts:
                    message = reply
                    break

        # If conversations_history returned the parent but we reacted to a reply,
        # the ts won't match - try conversations_replies with the message_ts as thread
        if not message or message.get("ts") != message_ts:
            # Try fetching as if message_ts could be a thread parent
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

        logging.info(f"[translation] Message ts: {message.get('ts')}, looking for: {message_ts}")
        logging.info(f"[translation] Message files: {[f.get('name') for f in message.get('files', [])]}")

        # Check if this message is in a thread (not the parent message)
        if not thread_ts or thread_ts == message_ts:
            logging.info(f"[translation] Message {message_ts} is not in a thread. Ignoring.")
            await client.chat_postMessage(
                channel=channel_id,
                text=":no_good: Сорі, переклад працює лише в треді. Постав емоджі на повідомлення з файлом всередині треду.",
                thread_ts=message_ts
            )
            return

        # Look for translatable files in the message
        files = message.get("files", [])
        srt_files = [f for f in files if f.get("name", "").lower().endswith(".srt")]
        txt_files = [f for f in files if f.get("name", "").lower().endswith(".txt")]

        if srt_files:
            for srt_file in srt_files:
                await process_translation_request(
                    srt_file, user_id, channel_id, thread_ts, client
                )
        elif txt_files:
            for txt_file in txt_files:
                await process_txt_translation_request(
                    txt_file, channel_id, thread_ts, client
                )
        else:
            logging.info(f"[translation] No .srt or .txt files found in message {message_ts}")
            await client.chat_postMessage(
                channel=channel_id,
                text=":no_good: Сорі, це ніби не файл для перекладу. Мені потрібен файл з розширенням `.srt` або `.txt`.",
                thread_ts=thread_ts
            )
            return

    except Exception as e:
        logging.error(f"[translation] Error handling reaction: {e}", exc_info=True)


async def process_translation_request(file_info: dict, user_id: str, channel_id: str, thread_ts: str, client):
    """Download, translate, and upload an SRT file."""
    file_id = file_info.get("id")
    file_name = file_info.get("name", "subtitles.srt")
    base_name = Path(file_name).stem

    logging.info(f"[translation:{file_id}] Starting translation for {file_name}")

    # Send confirmation message
    await client.chat_postMessage(
        channel=channel_id,
        text=f":saluting_face: Забираю в роботу переклад субтитрів `{file_name}`. Відпишу тобі, коли буде готово.",
        thread_ts=thread_ts
    )

    try:
        # Get fresh file info with download URL
        fresh_file_info = (await client.files_info(file=file_id))["file"]
        url_private = fresh_file_info.get("url_private")

        if not url_private:
            raise ValueError("Could not get file download URL")

        # Download the file
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            local_path = temp_dir_path / file_name

            logging.info(f"[translation:{file_id}] Downloading file")
            await download_file_streamed(url_private, local_path, client.token)

            # Read the SRT content
            async with aiofiles.open(local_path, "r", encoding="utf-8") as f:
                srt_content = await f.read()

            # Parse the SRT
            logging.info(f"[translation:{file_id}] Parsing SRT content")
            entries = parse_srt_content(srt_content)

            if not entries:
                raise ValueError("Could not parse SRT file - no valid entries found")

            # Extract texts for translation
            texts = [entry["text"] for entry in entries]
            logging.info(f"[translation:{file_id}] Translating {len(texts)} subtitle entries")

            # Translate using OpenAI
            translations = await translate_texts_with_openai(texts)

            # Rebuild SRT with translations
            logging.info(f"[translation:{file_id}] Rebuilding SRT with translations")
            translated_srt = rebuild_srt_with_translations(entries, translations)

            # Save translated SRT
            translated_filename = f"{base_name}-eng.srt"
            translated_path = temp_dir_path / translated_filename

            async with aiofiles.open(translated_path, "w", encoding="utf-8") as f:
                await f.write(translated_srt)

            # Upload to Slack
            logging.info(f"[translation:{file_id}] Uploading translated file")
            await client.files_upload_v2(
                channel=channel_id,
                file=str(translated_path),
                title=translated_filename,
                initial_comment=f":heavy_check_mark: Все вийшло, ось переклад субтитрів для файлу `{file_name}`.",
                thread_ts=thread_ts
            )

            logging.info(f"[translation:{file_id}] Translation complete")

    except Exception as e:
        logging.error(f"[translation:{file_id}] Error: {e}", exc_info=True)
        await client.chat_postMessage(
            channel=channel_id,
            text=f":pensive: Сорі, не вдалося перекласти `{file_name}`. Помилка: {e}",
            thread_ts=thread_ts
        )


DRIVE_LINK_PATTERNS = [
    re.compile(r'drive\.google\.com/file/d/([A-Za-z0-9_-]+)'),
    re.compile(r'docs\.google\.com/document/d/([A-Za-z0-9_-]+)'),
    re.compile(r'drive\.google\.com/open\?id=([A-Za-z0-9_-]+)'),
]


async def _find_drive_doc(file_id: str, base_name: str, channel_id: str, thread_ts: str, client) -> str | None:
    """
    Find the Google Drive file ID for the .docx corresponding to a transcript.
    Strategy:
      1. Search the Transponster shared drive by filename (scoped, handles the normal case)
      2. Fall back to scraping thread messages for any Drive link (handles renamed files)
    Returns the Drive file ID or None.
    """
    docx_name = f"{base_name}.docx"
    drive_service = get_google_drive_service()

    # --- Strategy 1: search Transponster shared drive by filename ---
    if drive_service:
        try:
            shared_drive_id = get_or_create_shared_drive(drive_service)
            if shared_drive_id:
                query = f"name='{docx_name}' and trashed=false"
                results = drive_service.files().list(
                    q=query,
                    corpora='drive',
                    driveId=shared_drive_id,
                    fields='files(id)',
                    orderBy='createdTime desc',
                    pageSize=1,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True
                ).execute()
                files_found = results.get('files', [])
                if files_found:
                    drive_file_id = files_found[0]['id']
                    logging.info(f"[txt-translation:{file_id}] Found Drive doc '{docx_name}' (ID: {drive_file_id}) on shared drive")
                    return drive_file_id
                else:
                    logging.info(f"[txt-translation:{file_id}] No doc named '{docx_name}' on shared drive, trying thread fallback")
        except Exception as e:
            logging.warning(f"[txt-translation:{file_id}] Drive filename search failed: {e}")

    # --- Strategy 2: scrape thread messages for any Drive link ---
    try:
        replies_result = await client.conversations_replies(
            channel=channel_id,
            ts=thread_ts
        )
        for msg in replies_result.get("messages", []):
            # Check text field
            for text_source in (msg.get("text", ""),):
                for pattern in DRIVE_LINK_PATTERNS:
                    match = pattern.search(text_source)
                    if match:
                        drive_file_id = match.group(1)
                        logging.info(f"[txt-translation:{file_id}] Found Drive ID '{drive_file_id}' in thread message text")
                        return drive_file_id
            # Check attachments
            for att in msg.get("attachments", []):
                for field in ("text", "fallback", "from_url", "original_url", "title_link"):
                    val = att.get(field, "")
                    for pattern in DRIVE_LINK_PATTERNS:
                        match = pattern.search(val)
                        if match:
                            drive_file_id = match.group(1)
                            logging.info(f"[txt-translation:{file_id}] Found Drive ID '{drive_file_id}' in thread attachment")
                            return drive_file_id
    except Exception as e:
        logging.warning(f"[txt-translation:{file_id}] Thread scraping fallback failed: {e}")

    logging.info(f"[txt-translation:{file_id}] No Drive doc found via filename or thread scraping")
    return None


async def process_txt_translation_request(file_info: dict, channel_id: str, thread_ts: str, client):
    """Download, translate, and upload a .txt transcript file. Optionally update the Drive .docx."""
    file_id = file_info.get("id")
    file_name = file_info.get("name", "transcript.txt")
    base_name = Path(file_name).stem

    logging.info(f"[txt-translation:{file_id}] Starting translation for {file_name}")

    await client.chat_postMessage(
        channel=channel_id,
        text=f":saluting_face: Забираю в роботу переклад розшифровки `{file_name}`. Відпишу тобі, коли буде готово.",
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

            # Parse the transcript
            logging.info(f"[txt-translation:{file_id}] Parsing transcript content")
            entries = parse_transcript_content(txt_content)

            if not entries:
                await client.chat_postMessage(
                    channel=channel_id,
                    text=f":no_good: Сорі, мені не вдалося зчитати файл `{file_name}`. Це не схоже на мою розшифровку. Я лиш свої умію.",
                    thread_ts=thread_ts
                )
                return

            # Extract texts and translate
            texts = [entry["text"] for entry in entries]
            logging.info(f"[txt-translation:{file_id}] Translating {len(texts)} transcript entries")
            translations = await translate_texts_with_openai(texts)

            # Rebuild transcript with translations
            logging.info(f"[txt-translation:{file_id}] Rebuilding transcript with translations")
            translated_txt = rebuild_transcript_with_translations(entries, translations)

            # Save and upload translated .txt
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

            logging.info(f"[txt-translation:{file_id}] Translation uploaded to Slack")

            # --- Google Drive update (non-fatal) ---
            try:
                if DEBUG and not DEBUG_GDRIVE:
                    logging.info(f"[txt-translation:{file_id}] Skipping Google Drive update (debug mode)")
                else:
                    drive_file_id = await _find_drive_doc(file_id, base_name, channel_id, thread_ts, client)
                    if drive_file_id:
                        drive_service = get_google_drive_service()
                        if drive_service:
                            doc_link = update_docx_with_translation(drive_service, drive_file_id, translated_txt)
                            if doc_link:
                                await client.chat_postMessage(
                                    channel=channel_id,
                                    text=f"📂 Переклад також додано до <{doc_link}|документа на Google Drive>.",
                                    thread_ts=thread_ts
                                )
                            else:
                                logging.warning(f"[txt-translation:{file_id}] update_docx_with_translation returned None")
                                await client.chat_postMessage(
                                    channel=channel_id,
                                    text=":information_desk_person: Мені не вдалося знайти документ на Google Drive для цієї розшифровки, тому я дам переклад лише тут у треді.",
                                    thread_ts=thread_ts
                                )
                    else:
                        await client.chat_postMessage(
                            channel=channel_id,
                            text=":information_desk_person: Мені не вдалося знайти документ на Google Drive для цієї розшифровки, тому я дам переклад лише тут у треді.",
                            thread_ts=thread_ts
                        )
            except Exception as e:
                logging.error(f"[txt-translation:{file_id}] Google Drive update failed: {e}", exc_info=True)

            logging.info(f"[txt-translation:{file_id}] Translation complete")

    except Exception as e:
        logging.error(f"[txt-translation:{file_id}] Error: {e}", exc_info=True)
        await client.chat_postMessage(
            channel=channel_id,
            text=f":pensive: Сорі, не вдалося перекласти `{file_name}`. Помилка: {e}",
            thread_ts=thread_ts
        )
