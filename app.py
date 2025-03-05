import os
import logging
import requests
import datetime

from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO)

# Slack credentials
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

# ElevenLabs API
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

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

# Initialize the Slack Bolt App
app = App(
    token=SLACK_BOT_TOKEN,
    signing_secret=SLACK_SIGNING_SECRET
)

# Flask setup for the SlackRequestHandler
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# -----------------------------------------
#   Helper Functions
# -----------------------------------------

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
    """
    Check if the file size is greater than 1000 MB (1,000,000,000 bytes).
    """
    size_bytes = fileinfo.get("size", 0)
    return size_bytes > 1000 * 1_000_000

def transcribe_file(file_path):
    """
    Sends the file to ElevenLabs for transcription.
    This is a placeholder function; adapt as needed per official ElevenLabs docs.

    If you're encountering SSL issues, you could temporarily add verify=False:
        response = requests.post(url, headers=headers, files=files, data=data, verify=False)
    """
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

    # Remove "verify=False" in production
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
    logging.info("Flow Step 0: file_shared event triggered.")

    file_id = event["file_id"]
    user_id = event["user_id"]
    channel_id = event.get("channel_id")

    # Retrieve file info from Slack
    logging.info("Flow Step 1: Retrieving file info.")
    file_info = client.files_info(file=file_id)["file"]
    logging.info(f"File info: {file_info}")

    # Figure out the thread_ts for the existing message so we can reply in-thread
    thread_ts = get_thread_ts(file_info, channel_id)
    logging.info(f"Derived thread_ts={thread_ts}")

    # 1) Check if audio/video and mention user
    logging.info("Flow Step 2: Checking if file is audio/video.")
    if not is_audio_or_video(file_info):
        
        client.chat_postMessage(
            channel=channel_id,
            text=f"<@{user_id}> The file you uploaded is NOT an audio/video format. Please upload an MP3, MP4, WAV, etc.",
            thread_ts=thread_ts
        )
        logging.info("File is not audio/video. Flow ended.")
        return

    # 2) Check file size and mention user
    logging.info("Flow Step 3: Checking if file is too large (>1000 MB).")
    if file_too_large(file_info):
        client.chat_postMessage(
            channel=channel_id,
            text=f"<@{user_id}> The file size is over 1000 MB. Please shrink the file before uploading.",
            thread_ts=thread_ts
        )
        logging.info("File is too large. Flow ended.")
        return

    # 3) File is valid and mention user
    logging.info("Flow Step 4: Downloading file & sending to ElevenLabs for transcription.")
    client.chat_postMessage(
        channel=channel_id,
        text=f"<@{user_id}> Your file is valid. Uploading to ElevenLabs for transcription...",
        thread_ts=thread_ts
    )

    # Download the file from Slack to a local temp path
    download_url = file_info["url_private"]
    local_file_path = f"/tmp/{file_info['id']}_{file_info['name']}"
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

    logging.info("Flow Step 4a: Downloading file from Slack.")
    r = requests.get(download_url, headers=headers)
    with open(local_file_path, "wb") as f:
        f.write(r.content)
    logging.info(f"File downloaded locally to {local_file_path}.")

    # 4) Send to ElevenLabs
    logging.info("Flow Step 5: Transcribing file with ElevenLabs.")
    try:
        response = transcribe_file(local_file_path)
    except requests.exceptions.SSLError as e:
        logging.error(f"SSL error when calling ElevenLabs: {e}")
        client.chat_postMessage(
            channel=channel_id,
            text=f"<@{user_id}> There was an SSL error calling ElevenLabs. Please check your environment or certificates.",
            thread_ts=thread_ts
        )
        return

    if response is None:
        # Just in case something weird happened
        client.chat_postMessage(
            channel=channel_id,
            text=f"<@{user_id}> Unexpected error: No response from ElevenLabs.",
            thread_ts=thread_ts
        )
        logging.error("No response from ElevenLabs. Flow ended.")
        return

    logging.info(f"Transcription response status: {response.status_code}, body: {response.text}")

    if response.status_code != 200:
        client.chat_postMessage(
            channel=channel_id,
            text=f"<@{user_id}> There was an error transcribing your file. Please try again later.",
            thread_ts=thread_ts
        )
        logging.error(f"Transcription failed. Status: {response.status_code}, Error: {response.text}")
        return

    # 5) If success, parse transcription text from the response
    logging.info("Flow Step 6: Processing transcription result.")
    transcription_result = response.json()
    transcribed_text = transcription_result.get("text", "No text returned from ElevenLabs.")

    # Use the helper function to create a formatted transcript file
    txt_file_path = write_transcript_file(transcription_result, file_info["name"])
    logging.info(f"Formatted transcription saved to {txt_file_path}.")


    # 6) Upload .txt file to Slack
    logging.info("Flow Step 7: Uploading .txt file to Slack.")
    try:
        client.files_upload_v2(
            channels=channel_id,
            file=txt_file_path,
            title="Transcription",
            initial_comment=f"<@{user_id}> Here is your transcription!",
            thread_ts=thread_ts
        )
        logging.info("Transcription .txt uploaded to Slack.")
    except Exception as e:
        logging.error(f"Error uploading .txt to Slack: {e}")
        client.chat_postMessage(
            channel=channel_id,
            text=f"<@{user_id}> Could not upload the transcription file. Error: {e}",
            thread_ts=thread_ts
        )
        return

    logging.info("Flow completed successfully.")

# -----------------------------------------
#   Flask Routes
# -----------------------------------------
@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


if __name__ == "__main__":
    # Start the Flask server on port 3000
    flask_app.run(host="0.0.0.0", port=3000)