import os
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
    transcribe_file, get_thread_ts, write_transcript_file,
    cleanup_temp_file, create_srt_from_json, create_transcript,
    get_google_drive_service, find_or_create_folder, upload_as_google_doc,
    get_or_create_shared_drive
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
