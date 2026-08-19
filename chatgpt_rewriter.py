"""ChatGPT Caption Rewriter.

Independent module for caption rewriting using ChatGPT.
Reuses the existing chatgpt.py implementation without duplicating
authentication, session management, or API logic.
"""

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# Singleton ChatGPT client for rewrite operations (separate from Telegram bot)
_client = None
# Lock for thread-safe client initialization
_client_lock = threading.Lock()
# Lock for thread-safe send_message calls (protects shared mutable state)
_send_lock = threading.Lock()

# ChatGPT-specific safety prompt (independent from Gemini)
_SAFETY_PROMPT = """
You are a professional product caption writer for Amazon Egypt products.
Rewrite the following product caption to make it more engaging and professional.

REQUIREMENTS:
- Keep the product title accurate
- Keep the price information intact
- Keep the Amazon URL intact
- Maintain Arabic language (Egyptian dialect preferred)
- Make the caption more engaging and marketing-friendly
- Keep emojis where appropriate
- Do not invent or add information not in the original caption
- Do not remove important product details
- Keep the caption under 1000 characters
- Preserve Amazon Resale / Used product status (do not convert Resale to NEW)
"""


def _extract_plain_text(text: str) -> str:
    """
    Extract plain text from ChatGPT response by stripping document markup.
    
    Removes ChatGPT UI document markers like:
    :::writing{variant="document" id="..."}
    ::::writing{variant="document" id="..."}
    ...
    :::
    
    The actual content is INSIDE the document block, so we extract it from there.
    
    Args:
        text: Raw ChatGPT response possibly containing document markup
        
    Returns:
        Plain text without document markup
    """
    lines = text.split('\n')
    extracted_lines = []
    in_document_block = False
    
    for line in lines:
        # Check for document block start (both ::: and ::::)
        if line.strip().startswith(':::writing') or line.strip().startswith('::::writing'):
            in_document_block = True
            continue
        
        # Check for document block end
        if line.strip() == ':::':
            in_document_block = False
            continue
        
        # Extract lines inside document blocks (that's where the content is)
        if in_document_block:
            extracted_lines.append(line)
        # Also keep lines outside blocks (if any)
        else:
            extracted_lines.append(line)
    
    return '\n'.join(extracted_lines).strip()


def _get_client():
    """Get or create the singleton ChatGPT client for rewrite operations."""
    global _client
    if _client is None:
        with _client_lock:
            # Double-check locking pattern
            if _client is None:
                from chatgpt_client import ChatGPT
                # Use separate config files for rewrite operations to avoid interference with bot
                _client = ChatGPT(config_file="chatgpt_rewrite_config.json", cookies_file="chatgpt_rewrite_cookies.json")
    return _client


def rewrite_caption(caption: str, db: Any, skip_cache: bool = False, log_prefix: str = "") -> str:
    """
    Rewrite caption using ChatGPT.

    Uses the ChatGPT client from chatgpt_client.py with separate config files
    to avoid interference with the Telegram bot's ChatGPT instance.
    Uses a singleton client instance to avoid recreating session/cookies on every request.
    Thread-safe: locks protect shared mutable state during send_message calls.

    Args:
        caption: Original caption to rewrite
        db: Database instance to read settings from
        skip_cache: If True, bypass cache and call API directly
        log_prefix: Prefix for log messages (e.g., "MANUAL POST", "SOURCE POST")

    Returns:
        Rewritten caption, or original caption if ChatGPT fails
    """
    logger.info(f"{log_prefix} → ENTERED chatgpt_rewriter.rewrite_caption")
    logger.info(f"{log_prefix} → CHATGPT REWRITE START")

    # Check if ChatGPT rewrite is enabled
    if not db.get_chatgpt_rewrite_enabled():
        logger.info(f"{log_prefix} → CHATGPT REWRITE SKIPPED: ChatGPT rewrite disabled")
        return caption

    # Check cache first (unless skip_cache is True)
    if not skip_cache:
        cached = db.get_ai_rewrite_cache(caption, "chatgpt")
        if cached:
            logger.info(f"{log_prefix} → CHATGPT CACHE HIT: returning cached rewrite")
            return cached
        logger.info(f"{log_prefix} → CHATGPT CACHE MISS: proceeding to API")

    # Get the ChatGPT-specific rewrite prompt
    admin_prompt = db.get_chatgpt_rewrite_prompt() or ""

    # Build the full prompt with ChatGPT safety prompt
    if admin_prompt:
        full_prompt = f"{_SAFETY_PROMPT}\n{admin_prompt}\n\n{caption}"
    else:
        full_prompt = f"{_SAFETY_PROMPT}\n\n{caption}"

    logger.info(f"{log_prefix} → CHATGPT REQUEST START")

    start_time = time.time()
    try:
        # Use singleton ChatGPT client (created once, reused for all rewrites)
        gpt = _get_client()

        # Send message with thread-safe lock
        # ChatGPT.send_message() modifies shared state (conduit_token, sentry_trace, etc.)
        # Lock prevents race conditions between concurrent rewrite requests
        with _send_lock:
            # conversation_id=None and parent_id=None ensure each rewrite is independent
            reply, new_cid, new_pid, model, error = gpt.send_message(
                full_prompt,
                conversation_id=None,
                parent_id=None,
                on_token=None,
                retry=True
            )

        elapsed = time.time() - start_time
        logger.info(f"{log_prefix} → CHATGPT REQUEST END elapsed={elapsed:.3f}s")

        if error:
            logger.warning(
                f"{log_prefix} → CHATGPT FAILED: {error}. "
                f"Falling back to original caption."
            )
            return caption

        if not reply or reply.strip() == "":
            logger.warning(
                f"{log_prefix} → CHATGPT FAILED: empty response. "
                f"Falling back to original caption."
            )
            return caption

        # Extract plain text from ChatGPT response (strip document markup)
        raw_length = len(reply)
        logger.info(f"{log_prefix} → CHATGPT RAW PREFIX: {repr(reply[:80])}")
        
        extracted_text = _extract_plain_text(reply)
        extracted_length = len(extracted_text)
        
        logger.info(
            f"{log_prefix} → CHATGPT EXTRACTION: "
            f"RAW_RESPONSE_LENGTH={raw_length} "
            f"EXTRACTED_TEXT_LENGTH={extracted_length} "
            f"EXTRACTION_METHOD=strip_document_markup"
        )
        
        logger.info(f"{log_prefix} → CHATGPT EXTRACTED PREFIX: {repr(extracted_text[:80])}")

        reply = extracted_text
        
        logger.info(f"{log_prefix} → CHATGPT RETURNED PREFIX: {repr(reply[:80])}")

        # Validate response (same validation as Gemini)
        validation_fail_reason = None
        if len(reply) < len(caption) * 0.5:
            validation_fail_reason = f"Too short ({len(reply)} vs {len(caption)} chars)"
        else:
            has_price = any(char.isdigit() for char in reply)
            has_url = "http" in reply or "amazon" in reply.lower()
            if not has_price:
                validation_fail_reason = "Missing price"
            elif not has_url:
                validation_fail_reason = "Missing URL"

        if validation_fail_reason:
            logger.warning(
                f"{log_prefix} → CHATGPT VALIDATION FAILED: {validation_fail_reason}. "
                f"Falling back to original caption."
            )
            return caption

        logger.info(f"{log_prefix} → CHATGPT SUCCESS: model={model} elapsed={elapsed:.3f}s")

        # Log original caption
        logger.info(
            f"{log_prefix} → ==================================================\n"
            f"{log_prefix} → ORIGINAL CAPTION\n"
            f"{log_prefix} → ==================================================\n"
            f"{log_prefix} → {caption}\n"
            f"{log_prefix} → =================================================="
        )

        # Log ChatGPT response
        logger.info(
            f"{log_prefix} → ==================================================\n"
            f"{log_prefix} → CHATGPT RAW RESPONSE\n"
            f"{log_prefix} → ==================================================\n"
            f"{log_prefix} → Model: {model}\n"
            f"{log_prefix} → \n"
            f"{log_prefix} → FULL RESPONSE:\n"
            f"{log_prefix} → {reply}\n"
            f"{log_prefix} → =================================================="
        )

        # Log final caption
        logger.info(
            f"{log_prefix} → ==================================================\n"
            f"{log_prefix} → FINAL CAPTION (rewritten)\n"
            f"{log_prefix} → ==================================================\n"
            f"{log_prefix} → {reply}\n"
            f"{log_prefix} → =================================================="
        )

        # Cache the result (unless skip_cache is True)
        if not skip_cache:
            db.set_ai_rewrite_cache(caption, reply, "chatgpt")

        return reply

    except Exception as e:
        elapsed = time.time() - start_time
        logger.exception(
            f"{log_prefix} → CHATGPT FAILED: exception after {elapsed:.3f}s. "
            f"Falling back to original caption."
        )
        return caption
