import os
import asyncio
import logging
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import aiofiles
from slack_bolt.async_app import AsyncApp

from config import SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET
from helpers import (
    is_audio_or_video, file_too_large, SUPPORTED_EXTENSIONS,
    transcribe_file, get_thread_ts, write_transcript_file,
    cleanup_temp_file, create_srt_from_json
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
BATCH_WINDOW_SECONDS = 3.0

# --- Ukrainian Pluralization Helper ---
def get_file_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "файл"
    if 2 <= count % 10 <= 4 and (count % 100 < 10 or count % 100 >= 20):
        return "файли"
    return "файлів"

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

async def generate_and_upload_results(mode: str, base_filename: str, result_data: dict, file_info: dict, channel_id: str, thread_ts: str, client):
    upload_tasks = []
    temp_files_to_clean = []

    try:
        if mode in ("srt_only", "both"):
            logging.info(f"[{file_info['id']}] 6a: Generating .srt file.")
            srt_path = Path(tempfile.gettempdir()) / f"{base_filename}.srt"
            srt_text = create_srt_from_json(result_data, max_chars=40, max_duration=4.0)
            async with aiofiles.open(srt_path, "w", encoding="utf-8") as f:
                await f.write(srt_text)
            temp_files_to_clean.append(srt_path)
            
            logging.info(f"[{file_info['id']}] 7a: Uploading .srt file to Slack.")
            upload_tasks.append(client.files_upload_v2(
                channel=channel_id,
                file=str(srt_path),
                title=f"{base_filename}.srt",
                initial_comment=f":heavy_check_mark: Все вийшло, ось субтитри для файлу `{file_info['name']}`.",
                thread_ts=thread_ts
            ))

        if mode in ("txt_only", "both"):
            logging.info(f"[{file_info['id']}] 6b: Generating .txt file.")
            txt_path_str = write_transcript_file(result_data, file_info["name"])
            txt_path = Path(txt_path_str)
            temp_files_to_clean.append(txt_path)

            logging.info(f"[{file_info['id']}] 7b: Uploading .txt file to Slack.")
            upload_tasks.append(client.files_upload_v2(
                channel=channel_id,
                file=str(txt_path),
                title=f"{base_filename}.txt",
                initial_comment=f":heavy_check_mark: Все вийшло, ось розшифровка для файлу `{file_info['name']}`.",
                thread_ts=thread_ts
            ))
        
        if upload_tasks:
            await asyncio.gather(*upload_tasks)
            logging.info(f"[{file_info['id']}] 8: Successfully uploaded results.")

    finally:
        for fpath in temp_files_to_clean:
            if fpath.exists():
                cleanup_temp_file(str(fpath))

async def process_single_file(file_id: str, channel_id: str, thread_ts: str, client):
    logging.info(f"[{file_id}] Starting async processing for single file.")
    file_info = {}

    try:
        logging.info(f"[{file_id}] 1: Retrieving file info.")
        file_info = (await client.files_info(file=file_id))["file"]

        if file_info.get('filetype') == 'quip':
            logging.info(f"[{file_id}] Ignoring Canvas file: {file_info.get('title')}")
            return
        
        logging.info(f"[{file_id}] 2: Checking if file is audio/video.")
        if not is_audio_or_video(file_info):
            extensions = "`" + "`, `".join(SUPPORTED_EXTENSIONS[:-1]) + "` або `" + SUPPORTED_EXTENSIONS[-1] + "`" if len(SUPPORTED_EXTENSIONS) > 1 else f"`{SUPPORTED_EXTENSIONS[0]}`"
            await client.chat_postMessage(channel=channel_id, text=f":no_good: Сорі, файл `{file_info['name']}` не аудіо і не відео. Таке я тобі не розшифрую. Будь ласка, дай мені файл у форматі ({extensions}).", thread_ts=thread_ts)
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
            transcription_result = await run_transcription(local_file_path)

            logging.info(f"[{file_id}] 6: Processing transcription result.")
            filename_lower = file_info["name"].lower()
            mode = "txt_only"
            if "subtitles" in filename_lower or "субтитри" in filename_lower: mode = "srt_only"
            elif "both" in filename_lower or "обидва" in filename_lower: mode = "both"
            logging.info(f"[{file_id}] Determined transcription mode: {mode}")
            
            base_filename = Path(file_info["name"]).stem
            logging.info(f"[{file_id}] 7: Generating and uploading results.")
            await generate_and_upload_results(mode, base_filename, transcription_result, file_info, channel_id, thread_ts, client)

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
        
    final_file_ids = batch_task_info["file_ids"]
    thread_ts = batch_task_info["thread_ts"]
    user_id, channel_id = batch_key # Unpack for logging
    
    file_count = len(final_file_ids)
    file_word = get_file_word(file_count)
    confirmation_text = f":saluting_face: Забираю в роботу {file_count} {file_word}. Відпишу тобі по кожному окремо, коли я буду готовий, або якщо поламаюся." if file_count > 1 else ":saluting_face: Забираю в роботу. Відпишу тобі, коли я буду готовий, або якщо поламаюся."

    await client.chat_postMessage(channel=channel_id, text=confirmation_text, thread_ts=thread_ts)
    logging.info(f"Processing batch of {file_count} files for {batch_key}")
    
    processing_tasks = [
        process_single_file(file_id, channel_id, thread_ts, client)
        for file_id in final_file_ids
    ]
    await asyncio.gather(*processing_tasks)

@app.event("file_shared")
async def handle_file_shared_events(event, client):
    file_id = event["file_id"]
    user_id = event["user_id"]
    channel_id = event.get("channel_id")

    if not channel_id:
        logging.warning(f"File {file_id} shared without channel_id. Ignoring.")
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
