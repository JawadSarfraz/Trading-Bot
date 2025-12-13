"""
Background email polling service
Runs every 10-15 minutes to check for new TradingView alerts
"""
import os
import asyncio
import logging
import time
from typing import Dict, Any
from dotenv import load_dotenv

from email_service import fetch_unread_emails, mark_uid_as_seen
from persistence import is_email_processed, mark_email_processed, prune_old_records

# Import execute_order dynamically to avoid circular import
def get_execute_order():
    """Get execute_order function from app module"""
    from app import execute_order
    return execute_order

load_dotenv()

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "600"))  # Default: 10 minutes

def process_email(uid: str, message_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Process a single email alert
    
    Args:
        message_id: Gmail Message-ID
        payload: Parsed JSON from email
    
    Returns:
        Processing result
    """
    # Check if already processed
    if is_email_processed(message_id):
        logger.info(f"Email already processed: {message_id}")
        # If it's still unread in Gmail, mark it seen so we don't keep reprocessing it
        try:
            mark_uid_as_seen(uid)
        except Exception:
            pass
        return {"status": "duplicate", "message": "Email already processed"}
    
    # Validate payload
    if not payload.get("side") or not payload.get("symbol_tv") or not payload.get("bar_ts"):
        logger.warning(f"Invalid payload in email {message_id}: {payload}")
        mark_email_processed(message_id, "", "", "", "invalid_payload")
        try:
            mark_uid_as_seen(uid)
        except Exception:
            pass
        return {"status": "error", "message": "Invalid payload"}
    
    # Execute order
    logger.info(f"Processing email alert: {message_id} - {payload.get('side')} {payload.get('symbol_tv')} bar_ts={payload.get('bar_ts')}")
    execute_order = get_execute_order()
    result = execute_order(payload)
    
    # Log result details for debugging
    status = result.get("status", "unknown")
    if status == "error":
        error_msg = result.get("message", "Unknown error")
        logger.error(f"Order execution failed: {error_msg} - Payload: {payload}")
    elif status == "stale_signal":
        logger.warning(f"Signal rejected (stale): {result.get('message', '')} - bar_ts={payload.get('bar_ts')}")
    elif status in ("simulated_ok", "ok"):
        logger.info(f"Order executed successfully: {status} - Symbol: {result.get('symbol')}, Side: {result.get('side')}, Order ID: {result.get('order_id')}")
    else:
        logger.warning(f"Order execution returned status: {status} - {result.get('message', '')}")
    
    # Mark as processed
    mark_email_processed(
        message_id,
        payload.get("bar_ts", ""),
        payload.get("symbol_tv", ""),
        payload.get("side", ""),
        status
    )

    # Only mark email as read after we have persisted the result
    try:
        mark_uid_as_seen(uid)
    except Exception as e:
        logger.warning(f"Failed to mark email as seen (uid={uid}): {e}")
    
    return result

def poll_emails_once():
    """Poll emails once and process them"""
    logger.info("Starting email poll...")
    
    try:
        # Fetch unread emails
        emails = fetch_unread_emails()
        
        if not emails:
            logger.info("No new emails found")
            return
        
        logger.info(f"Found {len(emails)} new email(s)")
        
        # Process each email
        for uid, message_id, payload, raw_body in emails:
            try:
                result = process_email(uid, message_id, payload)
                logger.info(f"Email {message_id} (uid={uid}) processed: {result.get('status')}")
            except Exception as e:
                logger.error(f"Error processing email {message_id}: {e}")
                mark_email_processed(message_id, "", "", "", f"error: {str(e)}")
                try:
                    mark_uid_as_seen(uid)
                except Exception:
                    pass
        
        # Prune old records periodically (every 100 polls or so)
        if int(time.time()) % (POLL_INTERVAL_SEC * 100) < POLL_INTERVAL_SEC:
            prune_old_records()
    
    except Exception as e:
        logger.error(f"Error in email poll: {e}")

async def poll_emails_loop():
    """Background loop to poll emails periodically"""
    logger.info(f"Email poller started. Polling every {POLL_INTERVAL_SEC} seconds")
    
    while True:
        try:
            poll_emails_once()
        except Exception as e:
            logger.error(f"Error in polling loop: {e}")
        
        await asyncio.sleep(POLL_INTERVAL_SEC)

def start_email_poller():
    """Start the email polling service (blocking)"""
    try:
        asyncio.run(poll_emails_loop())
    except KeyboardInterrupt:
        logger.info("Email poller stopped")

if __name__ == "__main__":
    # For testing
    logging.basicConfig(level=logging.INFO)
    start_email_poller()

