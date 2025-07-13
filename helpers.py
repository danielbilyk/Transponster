import os
import logging
import requests
import datetime
import json
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from config import (
    ELEVENLABS_STT_URL,
    ELEVENLABS_API_KEY,
    ELEVENLABS_MODEL_ID,
    ELEVENLABS_TAG_AUDIO_EVENTS,
    ELEVENLABS_DIARIZE,
    GOOGLE_APPLICATION_CREDENTIALS,
    GOOGLE_CREDENTIALS_JSON
)

SUPPORTED_EXTENSIONS = [".mp3", ".wav", ".mp4", ".m4a", ".flac", ".ogg", ".aac"]

def format_timestamp(seconds: float) -> str:
    """
    Convert float-second timestamp into "HH:MM:SS,mmm".
    """
    total_ms = int(seconds * 1_000)
    h = total_ms // 3_600_000
    m = (total_ms % 3_600_000) // 60_000
    s = (total_ms % 60_000) // 1_000
    ms = total_ms % 1_000
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

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

def create_srt_from_json(transcript_json: dict,
                         max_chars: int = 40,
                         max_duration: float = 4.0) -> str:
    """
    Build an SRT file from word-level JSON:
     - Gather words until you hit:
        • sentence-ending punctuation (.,!?),
        • OR accumulated text length > max_chars,
        • OR time span > max_duration seconds
     - Then flush that buffer as one subtitle block.
    Returns the full SRT as a single string.
    """
    segments = []
    buf = []

    for w in transcript_json.get("words", []):
        if w["type"] != "word":
            continue
        buf.append(w)
        text = " ".join([x["text"].strip() for x in buf]).strip()
        duration = buf[-1]["end"] - buf[0]["start"]

        # should we cut here?
        ends_sentence = buf[-1]["text"].rstrip().endswith((".", "!", "?"))
        if ends_sentence or len(text) >= max_chars or duration >= max_duration:
            segments.append(buf)
            buf = []

    # leftover
    if buf:
        segments.append(buf)

    # now render SRT
    out_lines = []
    for idx, seg in enumerate(segments, start=1):
        start_ts = format_timestamp(seg[0]["start"])
        end_ts   = format_timestamp(seg[-1]["end"])
        line_txt = " ".join([w["text"].strip() for w in seg]).strip()

        out_lines.append(str(idx))
        out_lines.append(f"{start_ts} --> {end_ts}")
        out_lines.append(line_txt)
        out_lines.append("")  # blank line

    return "\n".join(out_lines)

def write_transcript_file(transcription_result, original_filename, output_dir="/tmp"):
    base_filename = os.path.splitext(original_filename)[0]
    txt_file_path = os.path.join(output_dir, f"{base_filename}.txt")
    transcript = create_transcript(transcription_result)
    with open(txt_file_path, "w", encoding="utf-8") as f:
        f.write(transcript)
    return txt_file_path

def cleanup_temp_file(file_path):
    """Remove temporary file."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logging.info(f"Cleaned up temporary file: {file_path}")
    except Exception as e:
        logging.error(f"Error cleaning up temporary file {file_path}: {e}")

def is_audio_or_video(fileinfo):
    """Check if the file is audio or video
    based on Slack's 'mimetype' or 'filetype'."""
    mimetype = fileinfo.get("mimetype", "")
    if mimetype.startswith("audio") or mimetype.startswith("video"):
        return True
    # Fallback: check file extension if mimetype is generic
    filename = fileinfo.get("name", "").lower()
    return filename.endswith(tuple(SUPPORTED_EXTENSIONS))

def file_too_large(fileinfo):
    size_bytes = fileinfo.get("size", 0)
    return size_bytes > 1000 * 1_000_000

def transcribe_file(file_path: str, as_srt: bool = False):
    """
    Send `file_path` to ElevenLabs.
    - Returns JSON (dict) when as_srt=False (default).
    - Returns raw SRT text (str) when as_srt=True.
    """
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    data = {
        "model_id": ELEVENLABS_MODEL_ID,
        "tag_audio_events": ELEVENLABS_TAG_AUDIO_EVENTS,
        "diarize": ELEVENLABS_DIARIZE,
    }
    if as_srt:
        # request raw SRT from the API
        data["srt"] = json.dumps({"format": "srt"})

    with open(file_path, "rb") as fp:
        resp = requests.post(
            ELEVENLABS_STT_URL,
            headers=headers,
            files={"file": fp},
            data=data,
        )
    resp.raise_for_status()

    return resp.text if as_srt else resp.json()

def get_thread_ts(file_info, channel_id):
    # Extract the Slack thread_ts from the file info.
    shares = file_info.get("shares", {})
    thread_ts = None
    if "public" in shares and channel_id in shares["public"]:
        thread_ts = shares["public"][channel_id][0].get("ts")
    elif "private" in shares and channel_id in shares["private"]:
        thread_ts = shares["private"][channel_id][0].get("ts")
    return thread_ts

def get_google_drive_service():
    """Authenticate and return a Google Drive service object."""
    creds = None
    try:
        if GOOGLE_CREDENTIALS_JSON:
            logging.info("Attempting to create Google Drive service from GOOGLE_CREDENTIALS_JSON env var.")
            creds_json = json.loads(GOOGLE_CREDENTIALS_JSON)
            creds = service_account.Credentials.from_service_account_info(
                creds_json,
                scopes=['https://www.googleapis.com/auth/drive']
            )
        elif GOOGLE_APPLICATION_CREDENTIALS:
            logging.info(f"Attempting to create Google Drive service with credentials file: {GOOGLE_APPLICATION_CREDENTIALS}")
            creds = service_account.Credentials.from_service_account_file(
                GOOGLE_APPLICATION_CREDENTIALS,
                scopes=['https://www.googleapis.com/auth/drive']
            )
        
        if not creds:
            logging.error("Google Drive credentials are not configured. Please set either GOOGLE_CREDENTIALS_JSON or GOOGLE_APPLICATION_CREDENTIALS.")
            return None

        service = build('drive', 'v3', credentials=creds)
        logging.info("Successfully created Google Drive service")
        return service
    except json.JSONDecodeError:
        logging.error("Failed to parse GOOGLE_CREDENTIALS_JSON. Make sure it's valid JSON.")
        return None
    except Exception as e:
        logging.error(f"Failed to create Google Drive service: {e}")
        return None

def get_or_create_shared_drive(service, drive_name="Transponster"):
    """Get the existing Transponster shared drive."""
    try:
        # List all shared drives
        drives = service.drives().list().execute()
        
        # Look for existing Transponster drive
        for drive in drives.get('drives', []):
            if drive['name'] == drive_name:
                logging.info(f"Found existing shared drive: {drive['name']} (ID: {drive['id']})")
                return drive['id']
        
        # If no Transponster drive found, return None
        logging.warning(f"No shared drive named '{drive_name}' found")
        return None
        
    except HttpError as error:
        logging.error(f"An error occurred with shared drive: {error}")
        return None

def find_or_create_folder(service, folder_name, parent_id=None):
    """Find a folder by name, or create it if it doesn't exist. Returns (id, webViewLink, created_boolean)."""
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    
    try:
        # First, try to find the folder in shared drives
        response = service.files().list(
            q=query, 
            spaces='drive', 
            fields='files(id, name, webViewLink)',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        folders = response.get('files', [])
        
        if folders:
            return folders[0]['id'], folders[0]['webViewLink'], False # Existed
        
        # Create folder if not found
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        if parent_id:
            file_metadata['parents'] = [parent_id]
        
        folder = service.files().create(
            body=file_metadata, 
            fields='id, webViewLink',
            supportsAllDrives=True
        ).execute()
        return folder.get('id'), folder.get('webViewLink'), True # Created
    except HttpError as error:
        logging.error(f"An error occurred: {error}")
        return None, None, False

def upload_as_google_doc(service, file_name, file_content, folder_id):
    """Uploads text content as a .docx file to a specific folder."""
    try:
        from docx import Document
        from docx.shared import Pt
        from docx.oxml.ns import qn
        
        # Create a Word document
        doc = Document()
        p = doc.add_paragraph(file_content)
        for run in p.runs:
            run.font.name = "Montserrat"
            run.font.size = Pt(14)
            run._element.rPr.rFonts.set(qn('w:eastAsia'), 'Montserrat')
        
        # Create a temporary file to upload
        temp_file_path = f"/tmp/{file_name}.docx"
        doc.save(temp_file_path)

        file_metadata = {
            'name': f"{file_name}.docx",
            'parents': [folder_id],
            'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        }
        
        media = MediaFileUpload(temp_file_path, mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document', resumable=True)
        
        file = service.files().create(
            body=file_metadata, 
            media_body=media, 
            fields='id, webViewLink',
            supportsAllDrives=True
        ).execute()
        
        # Clean up the temporary file
        os.remove(temp_file_path)
        
        return file.get('webViewLink')
    except HttpError as error:
        logging.error(f"An error occurred during file upload: {error}")
        return None
    except ImportError:
        logging.error("python-docx library not available, falling back to .txt file")
        # Fallback to .txt if python-docx is not available
        temp_file_path = f"/tmp/{file_name}.txt"
        with open(temp_file_path, "w", encoding="utf-8") as f:
            f.write(file_content)

        file_metadata = {
            'name': f"{file_name}.txt",
            'parents': [folder_id],
            'mimeType': 'text/plain'
        }
        
        media = MediaFileUpload(temp_file_path, mimetype='text/plain', resumable=True)
        
        file = service.files().create(
            body=file_metadata, 
            media_body=media, 
            fields='id, webViewLink',
            supportsAllDrives=True
        ).execute()
        
        # Clean up the temporary file
        os.remove(temp_file_path)
        
        return file.get('webViewLink')