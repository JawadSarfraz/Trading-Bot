"""
Email service for fetching and parsing TradingView alerts from Gmail via IMAP
"""
import os
import json
import logging
import imaplib
import email
from email.header import decode_header
from typing import List, Dict, Optional, Tuple
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# IMAP configuration
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
IMAP_LABEL = os.getenv("IMAP_LABEL", "tv-alerts")  # Gmail label to read from
IMAP_FAILED_LABEL = os.getenv("IMAP_FAILED_LABEL", "tv-alerts-failed")  # Label for failed emails

def connect_imap() -> Optional[imaplib.IMAP4_SSL]:
    """Connect to IMAP server"""
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST)
        mail.login(IMAP_USER, IMAP_PASSWORD)
        return mail
    except Exception as e:
        logger.error(f"Failed to connect to IMAP: {e}")
        return None

def decode_mime_words(s: str) -> str:
    """Decode MIME encoded words in email headers"""
    decoded_parts = decode_header(s)
    decoded_str = ""
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            if encoding:
                decoded_str += part.decode(encoding)
            else:
                decoded_str += part.decode('utf-8', errors='ignore')
        else:
            decoded_str += part
    return decoded_str

def extract_json_from_email(body: str) -> Optional[Dict]:
    """Extract JSON payload from email body"""
    try:
        # Try to find JSON in the email body
        # TradingView emails might have JSON in the body
        lines = body.split('\n')
        json_str = None
        
        # Look for JSON object in body
        for line in lines:
            line = line.strip()
            if line.startswith('{') and line.endswith('}'):
                json_str = line
                break
        
        # If not found, try to extract from multi-line JSON
        if not json_str:
            # Find lines between { and }
            start_idx = body.find('{')
            end_idx = body.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                json_str = body[start_idx:end_idx + 1]
        
        if json_str:
            return json.loads(json_str)
        
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON from email: {e}")
        return None
    except Exception as e:
        logger.error(f"Error extracting JSON from email: {e}")
        return None

def fetch_unread_emails() -> List[Tuple[str, Dict, str]]:
    """
    Fetch unread emails from Gmail label
    
    Returns:
        List of tuples: (message_id, parsed_json, raw_body)
    """
    if not IMAP_USER or not IMAP_PASSWORD:
        logger.error("IMAP credentials not configured")
        return []
    
    mail = connect_imap()
    if not mail:
        return []
    
    try:
        # Select the label/folder
        if IMAP_LABEL:
            status, messages = mail.select(f'"{IMAP_LABEL}"')
        else:
            status, messages = mail.select("INBOX")
        
        if status != "OK":
            logger.error(f"Failed to select folder: {IMAP_LABEL}")
            mail.close()
            mail.logout()
            return []
        
        # Search for unread emails
        status, message_numbers = mail.search(None, "UNSEEN")
        
        if status != "OK":
            logger.warning("No unread emails found")
            mail.close()
            mail.logout()
            return []
        
        email_list = []
        message_ids = message_numbers[0].split()
        
        for msg_num in message_ids:
            try:
                # Fetch email
                status, msg_data = mail.fetch(msg_num, "(RFC822)")
                if status != "OK":
                    continue
                
                raw_email = msg_data[0][1]
                email_message = email.message_from_bytes(raw_email)
                
                # Get Message-ID
                message_id = email_message.get("Message-ID", "").strip()
                if not message_id:
                    # Use email UID as fallback
                    message_id = msg_num.decode()
                
                # Get email body
                body = ""
                if email_message.is_multipart():
                    for part in email_message.walk():
                        content_type = part.get_content_type()
                        if content_type == "text/plain" or content_type == "text/html":
                            try:
                                payload = part.get_payload(decode=True)
                                if payload:
                                    body += payload.decode('utf-8', errors='ignore')
                            except Exception:
                                pass
                else:
                    try:
                        payload = email_message.get_payload(decode=True)
                        if payload:
                            body = payload.decode('utf-8', errors='ignore')
                    except Exception:
                        body = str(email_message.get_payload())
                
                # Extract JSON from body
                parsed_json = extract_json_from_email(body)
                
                if parsed_json:
                    email_list.append((message_id, parsed_json, body))
                    logger.info(f"Parsed email: Message-ID={message_id}, Symbol={parsed_json.get('symbol_tv')}, Side={parsed_json.get('side')}")
                else:
                    logger.warning(f"Could not extract JSON from email: Message-ID={message_id}")
                    # Move to failed label if configured
                    if IMAP_FAILED_LABEL:
                        try:
                            mail.copy(msg_num, f'"{IMAP_FAILED_LABEL}"')
                        except Exception as e:
                            logger.error(f"Failed to move email to failed label: {e}")
            
            except Exception as e:
                logger.error(f"Error processing email {msg_num}: {e}")
                continue
        
        mail.close()
        mail.logout()
        
        return email_list
    
    except Exception as e:
        logger.error(f"Error fetching emails: {e}")
        try:
            mail.close()
            mail.logout()
        except Exception:
            pass
        return []

def mark_email_as_read(message_id: str) -> bool:
    """
    Mark email as read by Message-ID
    Note: This is a simplified version. In practice, you'd need to track
    the email UID from the fetch operation.
    """
    # For now, we'll mark emails as read during the fetch process
    # This function is a placeholder for future enhancement
    return True

def mark_emails_as_read(message_numbers: List[bytes], mail: imaplib.IMAP4_SSL) -> bool:
    """Mark multiple emails as read"""
    try:
        for msg_num in message_numbers:
            mail.store(msg_num, '+FLAGS', '\\Seen')
        return True
    except Exception as e:
        logger.error(f"Failed to mark emails as read: {e}")
        return False

