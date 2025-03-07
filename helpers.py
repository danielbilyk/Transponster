import os
import logging
import datetime
import requests

def get_file_duration_seconds(file_info):
    """Extract file duration in seconds from file metadata if available."""
    duration_ms = file_info.get("duration_ms")
    if duration_ms:
        return duration_ms / 1000

    # Fallback: estimate based on file size (1MB ~= 1 minute)
    size_mb = file_info.get("size", 0) / 1_000_000
    logging.info(f"No duration found, estimating {size_mb:.2f} minutes based on file size")
    return size_mb * 60

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
    return filename.endswith((".mp3", ".wav", ".mp4", ".m4a", ".flac", ".ogg"))

def file_too_large(fileinfo):
    size_bytes = fileinfo.get("size", 0)
    return size_bytes > 1000 * 1_000_000

def transcribe_file(file_path, api_key):
    
    """Send the file to ElevenLabs for transcription."""
    url = "https://api.elevenlabs.io/v1/speech-to-text"
    headers = {"xi-api-key": api_key,}
    files = {"file": open(file_path, "rb")}
    data = {
        "model_id": "scribe_v1",
        "tag_audio_events": 'true',
        "diarize": 'true',
    }

    response = requests.post(url, headers=headers, files=files, data=data)
    return response

def get_thread_ts(file_info, channel_id):
    # Extract the Slack thread_ts from the file info.
    shares = file_info.get("shares", {})
    thread_ts = None
    if "public" in shares and channel_id in shares["public"]:
        thread_ts = shares["public"][channel_id][0].get("ts")
    elif "private" in shares and channel_id in shares["private"]:
        thread_ts = shares["private"][channel_id][0].get("ts")
    return thread_ts