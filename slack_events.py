import os
import time
import logging
import requests
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from config import (
    SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET,
    ELEVENLABS_API_KEY, METRICS_FILE
)
from metrics import MetricsManager
from helpers import (
    get_file_duration_seconds, is_audio_or_video, file_too_large,
    transcribe_file, get_thread_ts, write_transcript_file, cleanup_temp_file
)

# Initialize MetricsManager and Slack app
metrics_manager = MetricsManager(METRICS_FILE)
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

    try:
        # Get user and file info
        user_info = client.users_info(user=user_id)
        username = user_info["user"]["name"]
        logging.info("1: Retrieving file info.")
        file_info = client.files_info(file=file_id)["file"]
        logging.info(f"File info: {file_info}")

        # Get file duration in seconds
        file_duration_seconds = get_file_duration_seconds(file_info)
        logging.info(f"File duration: {file_duration_seconds:.2f} seconds")

        thread_ts = get_thread_ts(file_info, channel_id)
        logging.info(f"Derived thread_ts={thread_ts}")

        # Validate file type
        logging.info("2: Checking if file is audio/video.")
        if not is_audio_or_video(file_info):
            client.chat_postMessage(
                channel=channel_id,
                text=f":no_good: Сорі, це не аудіо і не відео. Таке я тобі не розшифрую. Будь ласка, дай мені файл у форматі `.mp3`, `.wav`, `.m4a`, `.flac` чи `.ogg`.",
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
            response = transcribe_file(local_file_path, ELEVENLABS_API_KEY)
        except Exception as e:
            logging.error(f"Error when calling ElevenLabs: {e}")
            client.chat_postMessage(
                channel=channel_id,
                text=f":expressionless: Сорі, у мене не вийшло відправити запит до ElevenLabs: {str(e)}",
                thread_ts=thread_ts
            )
            metrics_manager.update_user_metrics(user_id, username, file_duration_seconds, success=False, processing_time=time.time() - start_time)
            return

        if response is None or response.status_code != 200:
            client.chat_postMessage(
                channel=channel_id,
                text=":expressionless: Failed to transcribe the file.",
                thread_ts=thread_ts
            )
            logging.error(f"Transcription failed. Status: {response.status_code if response else 'None'}, Error: {response.text if response else 'No response'}")
            metrics_manager.update_user_metrics(user_id, username, file_duration_seconds, success=False, processing_time=time.time() - start_time)
            return

        # Process and upload transcription result
        logging.info("6: Processing transcription result.")
        transcription_result = response.json()
        txt_file_path = write_transcript_file(transcription_result, file_info["name"])
        logging.info(f"Formatted transcription saved to {txt_file_path}.")

        logging.info("7: Uploading .txt file to Slack.")
        try:
            client.files_upload_v2(
                channel=channel_id,
                file=txt_file_path,
                title="Transcription",
                initial_comment=f":heavy_check_mark: Все вийшло, осьо твоя розшифровка.",
                thread_ts=thread_ts
            )
            logging.info("Transcription .txt uploaded to Slack.")

            processing_time = time.time() - start_time

            metrics_manager.update_user_metrics(user_id, username, file_duration_seconds, success=True, processing_time=processing_time)
            logging.info(f"Flow completed successfully in {processing_time:.2f} seconds.")

        except Exception as e:
            logging.error(f"Error uploading .txt to Slack: {e}")
            client.chat_postMessage(
                channel=channel_id,
                text=f":expressionless: Сорі, я не зміг завантажити сюди файл розшифровки. Помилка наступна: {e}",
                thread_ts=thread_ts
            )
            metrics_manager.update_user_metrics(user_id, username, file_duration_seconds, success=False, processing_time=time.time() - start_time)

    except Exception as e:
        logging.error(f"Unexpected error in handle_file_shared_events: {e}")
        try:
            metrics_manager.update_user_metrics(user_id, username, file_duration_seconds, success=False, processing_time=time.time() - start_time)
        except Exception as metrics_error:
            logging.error(f"Could not update metrics after unexpected error: {metrics_error}")
            
    finally:
        if local_file_path and os.path.exists(local_file_path):
            cleanup_temp_file(local_file_path)
        if 'file_info' in locals():
            txt_file_path_to_clean = f"/tmp/{file_info['name'].split('.')[0]}.txt"
            if os.path.exists(txt_file_path_to_clean):
                cleanup_temp_file(txt_file_path_to_clean)