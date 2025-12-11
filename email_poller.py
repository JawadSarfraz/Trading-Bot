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

from email_service import fetch_unread_emails
from persistence import is_email_processed, mark_email_processed, prune_old_records

# Import execute_order dynamically to avoid circular import
def get_execute_order():
    """Get execute_order function from app module"""
    from app import execute_order
    return execute_order

load_dotenv()

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "600"))  # Default: 10 minutes

def process_email(message_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
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
        return {"status": "duplicate", "message": "Email already processed"}
    
    # Validate payload
    if not payload.get("side") or not payload.get("symbol_tv") or not payload.get("bar_ts"):
        logger.warning(f"Invalid payload in email {message_id}: {payload}")
        mark_email_processed(message_id, "", "", "", "invalid_payload")
        return {"status": "error", "message": "Invalid payload"}
    
    # Execute order
    logger.info(f"Processing email alert: {message_id} - {payload.get('side')} {payload.get('symbol_tv')}")
    execute_order = get_execute_order()
    result = execute_order(payload)
    
    # Mark as processed
    status = result.get("status", "unknown")
    mark_email_processed(
        message_id,
        payload.get("bar_ts", ""),
        payload.get("symbol_tv", ""),
        payload.get("side", ""),
        status
    )
    
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
        for message_id, payload, raw_body in emails:
            try:
                result = process_email(message_id, payload)
                logger.info(f"Email {message_id} processed: {result.get('status')}")
            except Exception as e:
                logger.error(f"Error processing email {message_id}: {e}")
                mark_email_processed(message_id, "", "", "", f"error: {str(e)}")
        
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

