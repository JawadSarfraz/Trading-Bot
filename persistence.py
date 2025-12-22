"""
Persistence layer for tracking processed emails (idempotency)
Uses SQLite for simple, file-based storage
"""
import os
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Database file path (will be mounted volume in production)
DB_PATH = os.getenv("PERSISTENCE_DB_PATH", "processed_emails.db")
PRUNE_DAYS = int(os.getenv("PRUNE_DAYS", "30"))  # Keep records for 30 days

def get_db_connection():
    """Get SQLite database connection"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_emails (
            message_id TEXT PRIMARY KEY,
            bar_ts TEXT,
            symbol_tv TEXT,
            side TEXT,
            processed_at TEXT,
            result_status TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_processed_at 
        ON processed_emails(processed_at)
    """)
    conn.commit()
    return conn

def is_email_processed(message_id: str) -> bool:
    """Check if email has already been processed"""
    try:
        conn = get_db_connection()
        cursor = conn.execute(
            "SELECT 1 FROM processed_emails WHERE message_id = ?",
            (message_id,)
        )
        result = cursor.fetchone()
        conn.close()
        return result is not None
    except Exception as e:
        logger.error(f"Error checking processed email: {e}")
        return False

def mark_email_processed(message_id: str, bar_ts: str, symbol_tv: str, side: str, result_status: str = "ok"):
    """Mark email as processed"""
    try:
        conn = get_db_connection()
        processed_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO processed_emails 
               (message_id, bar_ts, symbol_tv, side, processed_at, result_status) 
               VALUES (?, ?, ?, ?, ?, ?)""",
            (message_id, bar_ts, symbol_tv, side, processed_at, result_status)
        )
        conn.commit()
        conn.close()
        logger.info(f"Marked email as processed: {message_id}")
    except Exception as e:
        logger.error(f"Error marking email as processed: {e}")

def prune_old_records():
    """Remove records older than PRUNE_DAYS"""
    try:
        conn = get_db_connection()
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=PRUNE_DAYS)
        cursor = conn.execute(
            "DELETE FROM processed_emails WHERE processed_at < ?",
            (cutoff_date.isoformat(),)
        )
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted > 0:
            logger.info(f"Pruned {deleted} old email records")
    except Exception as e:
        logger.error(f"Error pruning old records: {e}")

def get_processed_count() -> int:
    """Get count of processed emails"""
    try:
        conn = get_db_connection()
        cursor = conn.execute("SELECT COUNT(*) FROM processed_emails")
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logger.error(f"Error getting processed count: {e}")
        return 0

# P1: Persistent idempotency for TradingView signals (webhook + email)
def get_signal_db_connection():
    """Get SQLite database connection for signals"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_signals (
            signal_key TEXT PRIMARY KEY,
            exchange TEXT,
            symbol TEXT,
            side TEXT,
            timeframe TEXT,
            time_unix_ms TEXT,
            processed_at TEXT,
            result_status TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_processed_at 
        ON processed_signals(processed_at)
    """)
    conn.commit()
    return conn

def is_signal_processed(signal_key: str) -> bool:
    """Check if signal has already been processed"""
    try:
        conn = get_signal_db_connection()
        cursor = conn.execute(
            "SELECT 1 FROM processed_signals WHERE signal_key = ?",
            (signal_key,)
        )
        result = cursor.fetchone()
        conn.close()
        return result is not None
    except Exception as e:
        logger.error(f"Error checking processed signal: {e}")
        return False

def mark_signal_processed(signal_key: str, exchange: str, symbol: str, side: str, 
                          timeframe: str, time_unix_ms: str, result_status: str = "ok"):
    """Mark signal as processed"""
    try:
        conn = get_signal_db_connection()
        processed_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO processed_signals 
               (signal_key, exchange, symbol, side, timeframe, time_unix_ms, processed_at, result_status) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (signal_key, exchange, symbol, side, timeframe, time_unix_ms, processed_at, result_status)
        )
        conn.commit()
        conn.close()
        logger.debug(f"Marked signal as processed: {signal_key}")
    except Exception as e:
        logger.error(f"Error marking signal as processed: {e}")

def prune_old_signals():
    """Remove signal records older than PRUNE_DAYS"""
    try:
        conn = get_signal_db_connection()
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=PRUNE_DAYS)
        cursor = conn.execute(
            "DELETE FROM processed_signals WHERE processed_at < ?",
            (cutoff_date.isoformat(),)
        )
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted > 0:
            logger.info(f"Pruned {deleted} old signal records")
    except Exception as e:
        logger.error(f"Error pruning old signals: {e}")

