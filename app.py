import os
import logging
import requests
import datetime
import json
import time
from pathlib import Path

from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

# Logging setup
LOG_FORMAT = '%(asctime)s [%(levelname)s] %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

# Set up file logging
file_handler = logging.FileHandler("bot.log")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
logging.getLogger().addHandler(file_handler)

# Set up logging
logging.basicConfig(level=logging.INFO)

# Environment & Credentials
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
METRICS_FILE = os.getenv("METRICS_FILE", "transcription_metrics.json")

class MetricsManager:
    """Class to handle loading, updating, and saving transcription metrics."""
    def __init__(self, metrics_file):
        self.metrics_file = Path(metrics_file)
        self.metrics = self.load_metrics()

    def load_metrics(self):
        """Load metrics from JSON file if it exists; otherwise return default metrics."""
        if self.metrics_file.exists():
            try:
                with open(self.metrics_file, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logging.error(f"Error parsing metrics file {self.metrics_file}. Starting with fresh metrics.")
        # Default metrics structure
        return {
            "users": {},
            "total_files_processed": 0,
            "total_seconds_processed": 0,
            "transcription_success_rate": {
                "successful": 0,
                "failed": 0
            },
            "average_processing_time_seconds": {
                "total_time": 0,
                "count": 0
            },
            "average_file_length_seconds": {
                "total_length": 0,
                "count": 0
            }
        }

    def save_metrics(self):
        """Save current metrics to the JSON file."""
        self.metrics_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.metrics_file, 'w') as f:
            json.dump(self.metrics, f, indent=2)

    def update_user_metrics(self, user_id, username, file_duration_seconds, success=True, processing_time=None):
        """
        Update metrics for a specific user and global counters.
        Automatically saves the metrics after updating.
        """
        # Initialize user metrics if not already present
        if user_id not in self.metrics["users"]:
            self.metrics["users"][user_id] = {
                "username": username,
                "files_uploaded": 0,
                "total_seconds": 0,
                "file_durations": [],
                "success_rate": {
                    "successful": 0,
                    "failed": 0
                }
            }
            
        # Update per-user metrics
        self.metrics["users"][user_id]["files_uploaded"] += 1
        self.metrics["users"][user_id]["total_seconds"] += file_duration_seconds
        self.metrics["users"][user_id]["file_durations"].append(file_duration_seconds)

        # Update success/failure counts
        if success:
            self.metrics["users"][user_id]["success_rate"]["successful"] += 1
            self.metrics["transcription_success_rate"]["successful"] += 1
        else:
            self.metrics["users"][user_id]["success_rate"]["failed"] += 1
            self.metrics["transcription_success_rate"]["failed"] += 1

        # Update global metrics
        self.metrics["total_files_processed"] += 1
        self.metrics["total_seconds_processed"] += file_duration_seconds
        self.metrics["average_file_length_seconds"]["total_length"] += file_duration_seconds
        self.metrics["average_file_length_seconds"]["count"] += 1

        # Update processing time if provided
        if processing_time is not None:
            self.metrics["average_processing_time_seconds"]["total_time"] += processing_time
            self.metrics["average_processing_time_seconds"]["count"] += 1

        # Save updated metrics to file
        self.save_metrics()

# Instantiate the MetricsManager
metrics_manager = MetricsManager(METRICS_FILE)

def get_file_duration_seconds(file_info):
    """Extract file duration in seconds from file metadata if available."""
    # Try to get duration from file info
    duration_milliseconds = file_info.get("duration_ms")
    
    if duration_milliseconds:
        return duration_milliseconds / 1000
    
    # If no duration is available, estimate based on file size
    # Very rough estimate: 1MB ~= 1 minute of audio at medium quality
    size_mb = file_info.get("size", 0) / 1_000_000
    estimated_minutes = size_mb  # Simple 1:1 mapping
    
    logging.info(f"No duration found, estimating {estimated_minutes:.2f} minutes based on file size")
    return estimated_minutes

# Set up formatting
def format_timestamp(seconds):
    millis = int((seconds - int(seconds)) * 1000)
    time_str = str(datetime.timedelta(seconds=int(seconds)))
    parts = time_str.split(':')
    if len(parts) == 2:
        time_str = "0:" + time_str
    h, m, s = time_str.split(':')
    return f"{h.zfill(2)}:{m.zfill(2)}:{s.zfill(2)},{millis:03d}"

def create_transcript(transcription_result):
    words = transcription_result.get("words", [])
    if not words:
        return transcription_result.get("text", "")
    
    segments = []
    current_segment = []
    current_speaker = None

    for word in words:
        speaker = word.get("speaker_id", "unknown")
        if current_speaker is None:
            current_speaker = speaker
        elif speaker != current_speaker:
            segments.append((current_speaker, current_segment))
            current_segment = []
            current_speaker = speaker
        current_segment.append(word)
    
    if current_segment:
        segments.append((current_speaker, current_segment))
    
    transcript_lines = []
    for speaker, seg_words in segments:
        start_time = seg_words[0]["start"]
        end_time = seg_words[-1]["end"]
        start_formatted = format_timestamp(start_time)
        end_formatted = format_timestamp(end_time)
        segment_text = ''.join(word["text"] for word in seg_words)
        transcript_lines.append(f"{start_formatted} --> {end_formatted} - [{speaker}]")
        transcript_lines.append("")
        transcript_lines.append(segment_text)
        transcript_lines.append("")
    
    return "\n".join(transcript_lines)

def write_transcript_file(transcription_result, original_filename, output_dir="/tmp"):
    base_filename = os.path.splitext(original_filename)[0]
    txt_file_path = os.path.join(output_dir, f"{base_filename}.txt")
    transcript = create_transcript(transcription_result)
    with open(txt_file_path, "w", encoding="utf-8") as f:
        f.write(transcript)
    return txt_file_path

def cleanup_temp_file(file_path):
    """Remove temporary file after processing."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logging.info(f"Cleaned up temporary file: {file_path}")
    except Exception as e:
        logging.error(f"Error cleaning up temporary file {file_path}: {e}")

def is_audio_or_video(fileinfo):
    """
    Check if the file is audio or video based on Slack's 'mimetype' or 'filetype'.
    """
    mimetype = fileinfo.get("mimetype", "")
    if mimetype.startswith("audio") or mimetype.startswith("video"):
        return True
    # Fallback: check file extension if mimetype is generic
    filename = fileinfo.get("name", "").lower()
    if filename.endswith((".mp3", ".wav", ".mp4", ".m4a", ".flac", ".ogg")):
        return True
    return False

def file_too_large(fileinfo):

    # Check if the file size is greater than 1000 MB.
    size_bytes = fileinfo.get("size", 0)
    return size_bytes > 1000 * 1_000_000

def transcribe_file(file_path):
    
    # Sends the file to ElevenLabs for transcription.
    url = "https://api.elevenlabs.io/v1/speech-to-text"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
    }
    files = {
        "file": open(file_path, "rb")
    }
    data = {
        "model_id": "scribe_v1",
        "tag_audio_events": 'true',
        "diarize": 'true',
    }

    response = requests.post(url, headers=headers, files=files, data=data)
    return response

def get_thread_ts(file_info, channel_id):
    """
    Extracts the message 'thread_ts' from file_info for the given channel_id.
    Slack organizes 'shares' in 'public' or 'private' keys.
    """
    shares = file_info.get("shares", {})
    thread_ts = None

    # Check if the file was shared in a public or private channel
    if "public" in shares and channel_id in shares["public"]:
        thread_ts = shares["public"][channel_id][0].get("ts")
    elif "private" in shares and channel_id in shares["private"]:
        thread_ts = shares["private"][channel_id][0].get("ts")

    return thread_ts

# Initialize the Slack Bolt App
app = App(
    token=SLACK_BOT_TOKEN,
    signing_secret=SLACK_SIGNING_SECRET
)

# Flask setup for the SlackRequestHandler
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# -----------------------------------------
#   Slack Event Handlers
# -----------------------------------------

@app.event("file_shared")
def handle_file_shared_events(event, say, client):
    """
    1) Check if it's audio/video.
    2) Check size.
    3) If valid, mention user about uploading + call ElevenLabs.
    4) If transcription fails, mention user. Otherwise, provide .txt.
    """
    logging.info("file_shared event triggered.")

    # Start timing the process
    start_time = time.time()

    file_id = event["file_id"]
    user_id = event["user_id"]
    channel_id = event.get("channel_id")

    # Default values in case of early exceptions
    username = user_id  # Fallback if we can't get the real username
    file_duration_seconds = 0
    local_file_path = None  # Initialize for cleanup in finally block

    try:
        # Get user info to get username
        user_info = client.users_info(user=user_id)
        username = user_info["user"]["name"]
        
        # Retrieve file info from Slack
        logging.info("1: Retrieving file info.")
        file_info = client.files_info(file=file_id)["file"]
        logging.info(f"File info: {file_info}")
        
        # Get file duration in minutes
        file_duration_seconds = get_file_duration_seconds(file_info)
        logging.info(f"File duration: {file_duration_seconds:.2f} seconds")

        # Figure out the thread_ts for the existing message so we can reply in-thread
        thread_ts = get_thread_ts(file_info, channel_id)
        logging.info(f"Derived thread_ts={thread_ts}")

        # Check if audio/video
        logging.info("2: Checking if file is audio/video.")
        if not is_audio_or_video(file_info):
            client.chat_postMessage(
                channel=channel_id,
                text=f":no_good: Сорі, це не аудіо і не відео. Таке я тобі не розшифрую. Будь ласка, дай мені файл у форматі `.mp3`, `.wav`, `.m4a`, `.flac` чи `.ogg`.",
                thread_ts=thread_ts
            )
            logging.info("File is not audio/video. Flow ended.")

        # Check file size
        logging.info("3: Checking if file is too large (>1000 MB).")
        if file_too_large(file_info):
            client.chat_postMessage(
                channel=channel_id,
                text=f":no_good: Сорі, цей файл завеликий. Будь ласка, дай мені файл розміром до 1000 МБ.",
                thread_ts=thread_ts
            )
            logging.info("File is too large. Flow ended.")

        #File is valid
        logging.info("4: Downloading file & sending to ElevenLabs for transcription.")
        client.chat_postMessage(
            channel=channel_id,
            text=f":saluting_face: Забираю в роботу. Відпишу тобі, коли буду готовий, або якщо поламаюся.",
            thread_ts=thread_ts
        )

        # Download the file from Slack to a local temp path
        download_url = file_info["url_private"]
        local_file_path = f"/tmp/{file_info['id']}_{file_info['name']}"
        headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

        logging.info("4a: Downloading file from Slack.")
        r = requests.get(download_url, headers=headers)
        with open(local_file_path, "wb") as f:
            f.write(r.content)
        logging.info(f"File downloaded locally to {local_file_path}.")

        # 4) Send to ElevenLabs
        logging.info("5: Transcribing file with ElevenLabs.")
        try:
            response = transcribe_file(local_file_path)
        except requests.exceptions.SSLError as e:
            logging.error(f"SSL error when calling ElevenLabs: {e}")
            client.chat_postMessage(
                channel=channel_id,
                text=f":expressionless: Сорі, у мене не вийшло, тому що \"there was an SSL error calling ElevenLabs.\"",
                thread_ts=thread_ts
            )
            metrics_manager.update_user_metrics(user_id, username, file_duration_seconds, success=False, processing_time=time.time() - start_time)
            return

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
                text=f":expressionless: Сорі, я не зміг зробити розшифровку.",
                thread_ts=thread_ts
            )
            logging.error(f"Transcription failed. Status: {response.status_code if response else 'None'}, Error: {response.text if response else 'No response'}")
            metrics_manager.update_user_metrics(user_id, username, file_duration_seconds, success=False, processing_time=time.time() - start_time)
            return

        # 5) If success, parse transcription text from the response
        logging.info("6: Processing transcription result.")
        transcription_result = response.json()
        txt_file_path = write_transcript_file(transcription_result, file_info["name"])
        logging.info(f"Formatted transcription saved to {txt_file_path}.")

        # 6) Upload .txt file to Slack
        logging.info("Flow Step 7: Uploading .txt file to Slack.")
        try:
            client.files_upload_v2(
                channels=channel_id,
                file=txt_file_path,
                title="Розшифровка",
                initial_comment=f":heavy_check_mark: Все вийшло, осьо твоя розшифровка.",
                thread_ts=thread_ts
            )
            logging.info("Transcription .txt uploaded to Slack.")

            # Calculate processing time
            processing_time = time.time() - start_time
            
            # Update metrics for successful processing
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
        txt_file_path_to_clean = os.path.join("/tmp", f"{os.path.splitext(file_info['name'])[0]}.txt") if 'file_info' in locals() else None
        if txt_file_path_to_clean and os.path.exists(txt_file_path_to_clean):
            cleanup_temp_file(txt_file_path_to_clean)

# -----------------------------------------
#   Flask Route for Slack Events
# -----------------------------------------
@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=3000)