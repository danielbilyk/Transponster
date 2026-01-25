import os
from dotenv import load_dotenv

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_STT_URL = os.getenv(
    "ELEVENLABS_STT_URL",
    "https://api.elevenlabs.io/v1/speech-to-text"
)
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "scribe_v1")
ELEVENLABS_TAG_AUDIO_EVENTS = os.getenv("ELEVENLABS_TAG_AUDIO_EVENTS", "true")
ELEVENLABS_DIARIZE = os.getenv("ELEVENLABS_DIARIZE", "true")
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# Debug mode: disables Google Drive by default (use DEBUG_GDRIVE=true to re-enable)
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
DEBUG_GDRIVE = os.getenv("DEBUG_GDRIVE", "false").lower() == "true"

# OpenAI API key for translation
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")