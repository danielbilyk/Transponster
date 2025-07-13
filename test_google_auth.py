#!/usr/bin/env python3
"""
Test script to verify Google API authentication and scopes.
"""

import os
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_google_auth():
    """Test Google Drive API authentication and check for shared drives."""
    
    # Check if credentials file exists
    creds_path = "credentials.json"
    if not os.path.exists(creds_path):
        logger.error(f"Credentials file not found: {creds_path}")
        return False
    
    try:
        # Test Drive API
        logger.info("Testing Google Drive API...")
        creds = service_account.Credentials.from_service_account_file(
            creds_path,
            scopes=['https://www.googleapis.com/auth/drive']
        )
        drive_service = build('drive', 'v3', credentials=creds)
        
        # Try to list files (this will fail if no permissions, but won't fail if scopes are wrong)
        try:
            results = drive_service.files().list(pageSize=1).execute()
            logger.info("✅ Google Drive API authentication successful")
        except HttpError as e:
            if "insufficientFilePermissions" in str(e):
                logger.warning("⚠️  Drive API works but no file permissions (this is expected)")
            else:
                logger.error(f"❌ Drive API error: {e}")
                return False
        
        # Check for existing shared drives
        logger.info("Checking for existing shared drives...")
        try:
            drives = drive_service.drives().list().execute()
            if drives.get('drives'):
                logger.info("✅ Found shared drives:")
                for drive in drives['drives']:
                    logger.info(f"  - {drive['name']} (ID: {drive['id']})")
            else:
                logger.warning("⚠️  No shared drives found")
        except HttpError as e:
            logger.warning(f"⚠️  Cannot list shared drives: {e}")
        
        logger.info("🎉 Google Drive API test passed!")
        return True
        
    except Exception as e:
        logger.error(f"❌ Authentication failed: {e}")
        return False

if __name__ == "__main__":
    test_google_auth() 