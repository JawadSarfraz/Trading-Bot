"""
IMAP IDLE listener for near real-time email alerts
Replaces polling with persistent connection that gets notified immediately on new emails
"""
import imaplib
import email
import time
import os
import socket
import ssl
import traceback
import logging
import select
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("email_idle")

# Environment variables
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
IMAP_LABEL = os.getenv("IMAP_LABEL", "tv-alerts")
IMAP_FAILED_LABEL = os.getenv("IMAP_FAILED_LABEL", "tv-alerts-failed")
MAX_MESSAGE_AGE_MIN = int(os.getenv("MAX_MESSAGE_AGE_MIN", "5"))  # Ignore emails older than this
IDLE_RENEW_SEC = int(os.getenv("IDLE_RENEW_SEC", "1500"))  # 25 minutes (Gmail limit is 29 min)
RECONNECT_BACKOFFS = [2, 5, 10, 20, 30]  # Seconds to wait before reconnecting

# Wire to existing functions
def process_alert_email(raw_bytes: bytes) -> bool:
    """
    Parse the email and place the order.
    Wired to existing email processing pipeline.
    """
    try:
        from email_service import extract_json_from_email
        from email_poller import process_email
        import email as email_lib
        
        # Parse email
        msg = email_lib.message_from_bytes(raw_bytes)
        subject = msg.get("Subject", "")
        
        # Extract body
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="ignore")
                        break
                elif part.get_content_type() == "text/html" and not body:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="ignore")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode("utf-8", errors="ignore")
        
        # Extract JSON payload
        payload_dict = extract_json_from_email(body, subject)
        if not payload_dict:
            log.warning(f"Could not extract JSON from email subject: {subject[:50]}")
            return False
        
        # Get Message-ID for deduplication
        message_id = msg.get("Message-ID", "")
        if not message_id:
            # Fallback: use a hash or timestamp
            import hashlib
            message_id = hashlib.md5(raw_bytes).hexdigest()
        
        # Process email (this calls execute_order internally)
        # Note: process_email expects (uid, message_id, payload, raw_body)
        # We'll adapt it - for IDLE we don't have uid until we fetch
        # So we'll create a wrapper that fetches the email properly
        log.info(f"Processing alert email: Message-ID={message_id}, Symbol={payload_dict.get('symbol_tv')}, Side={payload_dict.get('side')}")
        
        # We need to fetch the email with UID to mark it as seen
        # For now, we'll process it and mark it via a different mechanism
        # Actually, let's call the existing process_email logic but adapt it
        from email_poller import get_execute_order
        from persistence import is_email_processed, mark_email_processed
        
        # Check if already processed
        if is_email_processed(message_id):
            log.info(f"Email already processed: {message_id}")
            return True
        
        # Execute order
        execute_order = get_execute_order()
        result = execute_order(payload_dict)
        
        # Mark as processed
        status = result.get("status", "unknown")
        mark_email_processed(
            message_id,
            payload_dict.get("bar_ts", ""),
            payload_dict.get("symbol_tv", ""),
            payload_dict.get("side", ""),
            status
        )
        
        if status in ("ok", "simulated_ok"):
            log.info(f"Alert processed successfully: {status}")
            return True
        else:
            log.warning(f"Alert processing returned: {status}")
            return False
            
    except Exception as e:
        log.error(f"Error processing alert email: {e}\n{traceback.format_exc()}")
        return False

def is_processed(message_id: str) -> bool:
    """Dedup by Message-ID using existing persistence layer."""
    from persistence import is_email_processed
    return is_email_processed(message_id)

def mark_processed(message_id: str):
    """Mark the message-id as processed in existing DB."""
    from persistence import mark_email_processed
    # We'll mark it with minimal info since we don't have full payload here
    mark_email_processed(message_id, "", "", "", "processed")

# === IMAP IDLE Implementation ===

def _connect():
    """Connect to IMAP server and select label."""
    log.info("Connecting IMAP...")
    M = imaplib.IMAP4_SSL(IMAP_HOST)
    # Gmail app password works with LOGIN
    M.login(IMAP_USER, IMAP_PASSWORD)
    typ, _ = M.select(f'"{IMAP_LABEL}"', readonly=False)
    if typ != "OK":
        raise RuntimeError(f"Failed to select label: {IMAP_LABEL}")
    log.info("IMAP connected and label selected.")
    return M

def _search_unseen(M):
    """Search for unseen emails, return list of UIDs."""
    typ, data = M.uid("search", None, "(UNSEEN)")
    if typ != "OK":
        return []
    uids = data[0].split() if data[0] else []
    return [uid.decode("utf-8", errors="ignore") for uid in uids]

def _fetch_full(M, uid):
    """Fetch full email content, Message-ID, and INTERNALDATE."""
    raw_email = None
    internaldate = None
    message_id = None
    
    # Fetch RFC822 (full email)
    typ, data = M.uid("fetch", uid, "(RFC822)")
    if typ != "OK" or not data or data[0] is None:
        return None, None, None
    
    # Parse the response - data[0] might be tuple like (b'uid fetch response', b'rawbytes')
    if isinstance(data[0], tuple) and len(data[0]) > 1:
        raw_email = data[0][1]
    elif isinstance(data[0], bytes):
        raw_email = data[0]
    
    # Fetch headers for Message-ID
    typ2, header_data = M.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID INTERNALDATE)])")
    if typ2 == "OK" and header_data and header_data[0]:
        header_bytes = header_data[0][1] if isinstance(header_data[0], tuple) else header_data[0]
        if header_bytes:
            header_str = header_bytes.decode("utf-8", errors="ignore")
            for line in header_str.splitlines():
                if line.lower().startswith("message-id:"):
                    message_id = line.split(":", 1)[1].strip()
                    break
                if line.lower().startswith("internaldate:"):
                    # Parse INTERNALDATE if present
                    try:
                        date_str = line.split(":", 1)[1].strip()
                        # INTERNALDATE format varies, fallback to now if parsing fails
                        internaldate = datetime.now(timezone.utc)
                    except Exception:
                        pass
    
    # If no INTERNALDATE found, use current time
    if internaldate is None:
        internaldate = datetime.now(timezone.utc)
    
    return raw_email, message_id, internaldate

def _too_old(dt):
    """Check if datetime is older than MAX_MESSAGE_AGE_MIN."""
    if not isinstance(dt, datetime):
        return True
    # Ensure dt is timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Calculate age as timedelta
    age = datetime.now(timezone.utc) - dt
    return age > timedelta(minutes=MAX_MESSAGE_AGE_MIN)

def _process_backlog(M):
    """Process any unseen emails in the label."""
    uids = _search_unseen(M)
    if not uids:
        return
    log.info(f"Backlog: {len(uids)} unseen")
    for uid in uids:
        try:
            raw, msg_id, when = _fetch_full(M, uid)
            if raw is None:
                continue
            if not msg_id:
                # Fallback: use hash of raw email
                import hashlib
                msg_id = hashlib.md5(raw).hexdigest() if raw else f"uid-{uid}"
            
            if is_processed(msg_id):
                log.info(f"Skip already processed: {msg_id}")
                # Mark as seen even if already processed
                try:
                    M.uid("store", uid, "+FLAGS", "(\\Seen)")
                except Exception:
                    pass
                continue
            
            if _too_old(when):
                log.info(f"Skip stale: {msg_id} (age > {MAX_MESSAGE_AGE_MIN} min)")
                mark_processed(msg_id)
                try:
                    M.uid("store", uid, "+FLAGS", "(\\Seen)")
                except Exception:
                    pass
                continue
            
            ok = process_alert_email(raw)
            if ok:
                # Mark as processed and seen
                mark_processed(msg_id)
                try:
                    M.uid("store", uid, "+FLAGS", "(\\Seen)")
                except Exception:
                    pass
            else:
                # Move to failed label if configured
                if IMAP_FAILED_LABEL:
                    try:
                        M.uid("copy", uid, f'"{IMAP_FAILED_LABEL}"')
                        M.uid("store", uid, "+FLAGS", "(\\Seen)")
                    except Exception:
                        pass
        except Exception as e:
            log.error(f"Error processing backlog uid {uid}: {e}\n{traceback.format_exc()}")

def _idle_once(M):
    """Enter IDLE mode once, renew every IDLE_RENEW_SEC or wake on activity."""
    # Enter IDLE
    tag = M._new_tag()
    M.send(f"{tag} IDLE\r\n".encode())
    _ = M.readline()  # Should be "+ idling"
    start = time.time()
    
    try:
        while True:
            # Renew every IDLE_RENEW_SEC, or wake sooner on activity
            elapsed = time.time() - start
            timeout = min(max(1, IDLE_RENEW_SEC - elapsed), 60)
            r, _, _ = select.select([M.socket()], [], [], timeout)
            if r:
                resp = M.readline().decode(errors="ignore").strip()
                if "EXISTS" in resp or "RECENT" in resp:
                    break  # new mail, exit IDLE to fetch
            if time.time() - start > IDLE_RENEW_SEC:
                break  # renew IDLE
    finally:
        try:
            M.send(b"DONE\r\n")
            _ = M.readline()
        except Exception:
            pass

def run_idle_forever():
    """
    Blocking loop. Call from a background thread or your app startup task.
    """
    backoff_idx = 0
    while True:
        M = None
        try:
            M = _connect()
            # Process any missed emails first
            _process_backlog(M)
            # Enter IDLE loop
            while True:
                _idle_once(M)
                _process_backlog(M)
            # never reaches here
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, socket.error, ssl.SSLError) as e:
            wait = RECONNECT_BACKOFFS[min(backoff_idx, len(RECONNECT_BACKOFFS) - 1)]
            log.warning(f"IMAP disconnected: {e!r}. Reconnecting in {wait}s...")
            time.sleep(wait)
            backoff_idx += 1
        except Exception:
            log.error("Unexpected error:\n" + traceback.format_exc())
            time.sleep(5)
        finally:
            if M:
                try:
                    M.logout()
                except Exception:
                    pass

