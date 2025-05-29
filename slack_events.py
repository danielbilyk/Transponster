import os
import time
import logging
import requests
import json
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from config import (
    SLACK_BOT_TOKEN,
    SLACK_SIGNING_SECRET,
    ELEVENLABS_API_KEY,
    ELEVENLABS_STT_URL,
    ELEVENLABS_MODEL_ID
)
from helpers import (
    is_audio_or_video, file_too_large, SUPPORTED_EXTENSIONS,
    transcribe_file, get_thread_ts, write_transcript_file,
    cleanup_temp_file, create_srt_from_json
)

# Initialize MetricsManager and Slack app
# metrics_manager = MetricsManager(METRICS_FILE)

app = App(
    token=SLACK_BOT_TOKEN,
    signing_secret=SLACK_SIGNING_SECRET
)
handler = SlackRequestHandler(app)

@app.event("file_shared")
def handle_file_shared_events(event, say, client):    
    """
    1) Validate if it's audio/video. Inform the user if not.
    2) Validate file size. Inform the user if too big.
    3) If all checks passed, inform the user about uploading to ElevenLabs & call the endpoint.
    4) If transcription fails, inform the user. Otherwise, provide transcription file.
    """
    logging.info("file_shared event triggered.")
    start_time = time.time()
    file_id = event["file_id"]
    user_id = event["user_id"]
    channel_id = event.get("channel_id")
    username = user_id  # Default if not found later
    local_file_path = None # Initialise to cleanup later
    txt_file_path   = None
    srt_file_path   = None

    try:
        # Get user and file info
        user_info = client.users_info(user=user_id)
        username = user_info["user"]["name"]
        logging.info("1: Retrieving file info.")
        file_info = client.files_info(file=file_id)["file"]
        logging.info(f"File info: {file_info}")

        # Skip Canvas files (type 'quip')
        if file_info['filetype'] == 'quip':
            logging.info(f"Ignoring Canvas file: {file_info['title']}")
            return

        thread_ts = get_thread_ts(file_info, channel_id)
        logging.info(f"Derived thread_ts={thread_ts}")

        logging.info("2: Checking if file is audio/video.")
        if not is_audio_or_video(file_info):
            # Join all extensions with commas, and make sure the last one is prefixed with 'або'
            if len(SUPPORTED_EXTENSIONS) > 1:
                extensions = "`" + "`, `".join(SUPPORTED_EXTENSIONS[:-1]) + "` або `" + SUPPORTED_EXTENSIONS[-1] + "`"
            else:
                extensions = "`" + SUPPORTED_EXTENSIONS[0] + "`"
            
            client.chat_postMessage(
                channel=channel_id,
                text=f":no_good: Сорі, це не аудіо і не відео. Таке я тобі не розшифрую. Будь ласка, дай мені файл у форматі {extensions}.",
                thread_ts=thread_ts
            )
            logging.info("File is not audio/video. Flow ended.")
            return

        # Validate file size
        logging.info("3: Checking if file is too large (>1000 MB).")
        if file_too_large(file_info):
            client.chat_postMessage(
                channel=channel_id,
                text=f":no_good: Сорі, цей файл завеликий. Будь ласка, дай мені файл розміром до 1000 МБ.",
                thread_ts=thread_ts
            )
            logging.info("File is too large. Flow ended.")
            return

        # Inform the user and download the file from Slack
        logging.info("4: Downloading file & sending to ElevenLabs for transcription.")
        client.chat_postMessage(
            channel=channel_id,
            text=f":saluting_face: Забираю в роботу. Відпишу тобі, коли буду готовий, або якщо поламаюся.",
            thread_ts=thread_ts
        )

        download_url = file_info["url_private"]
        local_file_path = f"/tmp/{file_info['id']}_{file_info['name']}"
        headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

        logging.info("4a: Downloading file from Slack.")
        r = requests.get(download_url, headers=headers)
        with open(local_file_path, "wb") as f:
            f.write(r.content)
        logging.info(f"File downloaded locally to {local_file_path}.")

        # Transcribe the file
        logging.info("5: Transcribing file with ElevenLabs.")
        try:
            transcription_result = transcribe_file(local_file_path)
        except Exception as e:
            logging.error(f"Error calling ElevenLabs: {e}")
            client.chat_postMessage(
                channel=channel_id,
                text=f":expressionless: Сорі, у мене не вийшло відправити запит до ElevenLabs: {str(e)}",
                thread_ts=thread_ts
            )
            return

        # Process and upload transcription result
        logging.info("6: Processing transcription result.")
        
        # decide transcription mode based on filename
        filename_lower = file_info["name"].lower()
        if "subtitles" in filename_lower or "субтитри" in filename_lower:
            mode = "srt_only"
        elif "both" in filename_lower or "обидва" in filename_lower:
            mode = "both"
        else:
            mode = "txt_only"
        logging.info(f"Determined transcription mode: {mode}")

        # define base name for both files
        base_filename = os.path.splitext(file_info["name"])[0]

        # 6a) If srt_only or both → hit ElevenLabs again to get raw .srt
        if mode in ("srt_only", "both"):
            logging.info("6a: Generating .srt file.")
            srt_file_path = f"/tmp/{file_info['id']}_{base_filename}.srt"
            srt_text = create_srt_from_json(transcription_result, max_chars=40, max_duration=4.0)
            with open(srt_file_path, "w", encoding="utf-8") as f:
                f.write(srt_text)
            logging.info(f"SRT transcription saved to {srt_file_path}.")

        # 6b) If txt_only or both → run existing txt logic
        if mode in ("txt_only", "both"):
            txt_file_path = write_transcript_file(transcription_result, file_info["name"])
            logging.info(f"Formatted transcription saved to {txt_file_path}.")

        # 7: Upload results to Slack according to mode
        if mode in ("srt_only", "both"):
            logging.info("7a: Uploading .srt file to Slack.")
            try:
                client.files_upload_v2(
                    channel=channel_id,
                    file=srt_file_path,
                    title=f"{base_filename}.srt",
                    initial_comment=":heavy_check_mark: Все вийшло, ось субтитри.",
                    thread_ts=thread_ts
                    )
                logging.info("SRT file uploaded to Slack.")

            except Exception as e:
                client.chat_postMessage(
                    channel=channel_id,
                    text=f":expressionless: Сорі, я не зміг завантажити файл субтитрів. Помилка: {e}",
                    thread_ts=thread_ts
                    )
                logging.error(f"Error uploading .srt to Slack: {e}")

        if mode in ("txt_only", "both"):
            logging.info("7b: Uploading .txt file to Slack.")
            try:
                client.files_upload_v2(
                    channel=channel_id,
                    file=txt_file_path,
                    title=f"{base_filename}.txt",
                    initial_comment=":heavy_check_mark: Все вийшло, ось твоя розшифровка.",
                    thread_ts=thread_ts
                    )
                logging.info("Transcription .txt uploaded to Slack.")

            except Exception as e:
                logging.error(f"Error in transcription flow: {e}")
                client.chat_postMessage(
                    channel=channel_id,
                    text=":expressionless: Сорі, щось пішло не так з транскрипцією.",
                    thread_ts=thread_ts
                    )
            
    finally:
        for path in (local_file_path, txt_file_path, srt_file_path):
            if path and os.path.exists(path):
                cleanup_temp_file(path)