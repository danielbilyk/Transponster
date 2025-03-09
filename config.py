import os
from dotenv import load_dotenv

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
METRICS_FILE = os.getenv("METRICS_FILE", "transcription_metrics.json")