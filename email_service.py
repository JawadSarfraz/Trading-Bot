"""
Email service for fetching and parsing TradingView alerts from Gmail via IMAP
"""
import os
import json
import logging
import imaplib
import email
import ast
import re
import html
from email.header import decode_header
from typing import List, Dict, Optional, Tuple, Any
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

def extract_json_from_email(body: str, subject: str = "") -> Optional[Dict]:
    """
    Extract JSON payload from TradingView email content.
    TradingView often includes the JSON in the email SUBJECT and/or in HTML body.
    """
    try:
        # Combine sources (subject first tends to be cleaner than HTML body)
        combined = "\n".join([subject or "", body or ""]).strip()
        if not combined:
            return None

        # HTML unescape (handles &quot; etc.) and normalize whitespace/smart punctuation
        combined = html.unescape(combined)
        combined = combined.replace("\u00a0", " ")  # nbsp
        combined = combined.translate(str.maketrans({
            "\u201c": "\"", "\u201d": "\"",  # curly double quotes
            "\u2018": "'", "\u2019": "'",    # curly single quotes
        }))

        # Pull JSON-ish blocks. Prefer ones that mention expected keys.
        candidates = re.findall(r"\{[\s\S]*?\}", combined)
        if not candidates:
            return None

        # Rank candidates: those containing 'secret' and 'symbol_tv' first
        def score(s: str) -> int:
            s_lower = s.lower()
            return int("secret" in s_lower) + int("symbol_tv" in s_lower) + int("side" in s_lower) + int("bar_ts" in s_lower)

        candidates.sort(key=score, reverse=True)

        for cand in candidates:
            s = cand.strip()
            # collapse excessive whitespace inside candidate
            s = re.sub(r"\s+", " ", s).strip()

            # First try strict JSON
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                pass

            # Fallback 0: some TV/Gmail renderers end up with semicolons instead of commas
            # Example: {"a":"b";"c":"d"}  ->  {"a":"b","c":"d"}
            if ";" in s and "," not in s:
                try:
                    return json.loads(s.replace(";", ","))
                except json.JSONDecodeError:
                    pass

            # Fallback 1: TradingView sometimes ends up showing dict-style payloads:
            # {'secret':"x", 'symbol_tv':"BYBIT:SOLUSDT.P", 'side':"long", 'bar_ts':"..."}
            # Try safe Python literal eval, then ensure it's a dict.
            try:
                obj = ast.literal_eval(s)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

        return None
    except Exception as e:
        logger.error(f"Error extracting JSON from email: {e}")
        return None

def _select_mailbox(mail: imaplib.IMAP4_SSL) -> bool:
    if IMAP_LABEL:
        status, _ = mail.select(f'"{IMAP_LABEL}"')
    else:
        status, _ = mail.select("INBOX")
    return status == "OK"


def fetch_unread_emails() -> List[Tuple[str, str, Dict[str, Any], str]]:
    """
    Fetch unread emails from Gmail label
    
    Returns:
        List of tuples: (uid, message_id, parsed_json, raw_body)
    """
    if not IMAP_USER or not IMAP_PASSWORD:
        logger.error("IMAP credentials not configured")
        return []
    
    mail = connect_imap()
    if not mail:
        return []
    
    try:
        if not _select_mailbox(mail):
            logger.error(f"Failed to select folder: {IMAP_LABEL}")
            mail.close()
            mail.logout()
            return []
        
        # Search for unread emails using UID (stable across sessions)
        status, data = mail.uid("search", None, "UNSEEN")
        
        if status != "OK":
            logger.warning("No unread emails found")
            mail.close()
            mail.logout()
            return []
        
        email_list = []
        uids = data[0].split()
        
        for uid_b in uids:
            uid = uid_b.decode("utf-8", errors="ignore")
            try:
                # Fetch email
                status, msg_data = mail.uid("fetch", uid, "(RFC822)")
                if status != "OK":
                    continue
                
                raw_email = msg_data[0][1]
                email_message = email.message_from_bytes(raw_email)
                
                # Get Message-ID
                message_id = email_message.get("Message-ID", "").strip()
                if not message_id:
                    # Use UID as fallback
                    message_id = uid
                
                # Subject (sometimes contains the JSON)
                subject = ""
                try:
                    subject_raw = email_message.get("Subject", "") or ""
                    subject = decode_mime_words(subject_raw)
                except Exception:
                    subject = ""

                # Get email body
                # Prefer text/plain; fallback to text/html if needed.
                body = ""
                if email_message.is_multipart():
                    plain_parts: list[str] = []
                    html_parts: list[str] = []
                    for part in email_message.walk():
                        content_type = part.get_content_type()
                        if content_type not in ("text/plain", "text/html"):
                            continue
                        try:
                            payload = part.get_payload(decode=True)
                            if not payload:
                                continue
                            decoded = payload.decode("utf-8", errors="ignore")
                            if content_type == "text/plain":
                                plain_parts.append(decoded)
                            else:
                                html_parts.append(decoded)
                        except Exception:
                            pass
                    body = "\n".join(plain_parts).strip() or "\n".join(html_parts).strip()
                else:
                    try:
                        payload = email_message.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="ignore")
                        else:
                            body = str(email_message.get_payload())
                    except Exception:
                        body = str(email_message.get_payload())
                
                # Extract JSON from subject/body
                parsed_json = extract_json_from_email(body=body, subject=subject)
                
                if parsed_json:
                    email_list.append((uid, message_id, parsed_json, body))
                    logger.info(
                        f"Parsed email: uid={uid}, Message-ID={message_id}, Symbol={parsed_json.get('symbol_tv')}, Side={parsed_json.get('side')}"
                    )
                else:
                    logger.warning(f"Could not extract JSON from email: Message-ID={message_id}")
                    # Move to failed label if configured
                    if IMAP_FAILED_LABEL:
                        try:
                            mail.uid("copy", uid, f'"{IMAP_FAILED_LABEL}"')
                            mail.uid("store", uid, "+FLAGS", "(\\Seen)")
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

def mark_uid_as_seen(uid: str) -> bool:
    """Mark a single email UID as seen."""
    mail = connect_imap()
    if not mail:
        return False
    try:
        if not _select_mailbox(mail):
            return False
        mail.uid("store", uid, "+FLAGS", "(\\Seen)")
        mail.close()
        mail.logout()
        return True
    except Exception as e:
        logger.error(f"Failed to mark uid as seen: {uid}: {e}")
        try:
            mail.close()
            mail.logout()
        except Exception:
            pass
        return False

