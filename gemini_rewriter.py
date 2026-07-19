import logging
import os
import time
from typing import Any

import google.generativeai as genai

from gemini_key_pool import get_key_pool

logger = logging.getLogger(__name__)

# Hidden safety prompt - always prepended to admin prompt
_SAFETY_PROMPT = """You are only rewriting the writing style.

Never change:
- prices
- coupon values
- discounts
- affiliate URLs
- Amazon URLs
- HTML tags
- product model numbers
- brand names
- numeric values

Do not invent information.
Do not remove information.
Only improve readability and marketing style.

"""


def _try_gemini_request(
    caption: str,
    model_name: str,
    admin_prompt: str,
    temperature: float,
    max_tokens: int,
    api_key: str,
    log_prefix: str,
) -> tuple[str, dict]:
    """
    Attempt a single Gemini API request with the given API key.
    
    Args:
        caption: Original caption to rewrite
        model_name: Gemini model name
        admin_prompt: Admin system prompt
        temperature: Temperature parameter
        max_tokens: Max tokens parameter
        api_key: API key to use
        log_prefix: Log message prefix
        
    Returns:
        Tuple of (rewritten_caption, metadata_dict)
        
    Raises:
        Exception: If the request fails for any reason
    """
    # Configure Gemini
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)

    # Build the request: Hidden Safety Prompt + Admin Prompt + User Message
    if admin_prompt:
        full_prompt = f"{_SAFETY_PROMPT}\n{admin_prompt}\n\n{caption}"
    else:
        full_prompt = f"{_SAFETY_PROMPT}\n\n{caption}"

    # Generate response
    generation_config = genai.types.GenerationConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
    )

    t0 = time.perf_counter()
    logger.info("%s → GEMINI API CALL START", log_prefix)
    response = model.generate_content(
        full_prompt,
        generation_config=generation_config,
    )
    elapsed = time.perf_counter() - t0
    logger.info("%s → GEMINI API CALL END elapsed=%.3fs", log_prefix, elapsed)

    # Extract rewritten caption
    rewritten = response.text.strip()

    # Get token counts and finish reason
    tokens_in = response.usage_metadata.prompt_token_count if hasattr(response, "usage_metadata") else "N/A"
    tokens_out = response.usage_metadata.candidates_token_count if hasattr(response, "usage_metadata") else "N/A"
    total_tokens = response.usage_metadata.total_token_count if hasattr(response, "usage_metadata") else "N/A"
    finish_reason = response.candidates[0].finish_reason if hasattr(response, "candidates") and response.candidates else "N/A"

    metadata = {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "total_tokens": total_tokens,
        "finish_reason": finish_reason,
    }

    return rewritten, metadata


def rewrite_caption(caption: str, db: Any, skip_cache: bool = False, log_prefix: str = "") -> str:
    """
    Rewrite caption using Gemini AI with automatic failover across multiple API keys.

    Args:
        caption: Original caption to rewrite
        db: Database instance to read settings from
        skip_cache: If True, bypass cache and call Gemini directly
        log_prefix: Prefix for log messages (e.g., "MANUAL POST", "SOURCE POST")

    Returns:
        Rewritten caption, or original caption if Gemini is disabled or all keys fail
    """
    logger.info(f"{log_prefix} → ENTERING GEMINI REWRITE")
    
    # Check if Gemini is enabled
    if not db.get_gemini_enabled():
        logger.info(f"{log_prefix} → AI REWRITE SKIPPED: Gemini disabled")
        return caption

    # Check cache first (unless skip_cache is True)
    if not skip_cache:
        cached = db.get_gemini_rewrite_cache(caption)
        if cached:
            logger.info(f"{log_prefix} → CACHE HIT: returning cached rewrite")
            return cached
        logger.info(f"{log_prefix} → CACHE MISS: proceeding to Gemini API")

    # Read settings from database
    model_name = db.get_gemini_model()
    admin_prompt = db.get_gemini_system_prompt()
    temperature = db.get_gemini_temperature()
    max_tokens = db.get_gemini_max_tokens()

    logger.info(
        f"{log_prefix} → AI REWRITE START: model=%s temperature=%s max_tokens=%s",
        model_name,
        temperature,
        max_tokens,
    )

    start_time = time.time()
    key_pool = get_key_pool()
    logger.info(f"{log_prefix} → ACQUIRING GEMINI KEY POOL")

    # Try each available key
    keys_attempted = []
    last_error = None

    while True:
        # Get next healthy key
        key = key_pool.get_next_key()
        if key is None:
            logger.error(f"{log_prefix} → ALL GEMINI KEYS UNAVAILABLE")
            break

        keys_attempted.append(key.index)
        logger.info(f"{log_prefix} → ATTEMPTING REQUEST WITH KEY #{key.index}")

        try:
            # Attempt the request
            rewritten, metadata = _try_gemini_request(
                caption=caption,
                model_name=model_name,
                admin_prompt=admin_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                api_key=key.api_key,
                log_prefix=log_prefix,
            )

            duration_ms = int((time.time() - start_time) * 1000)

            # Log original caption (complete, no truncation)
            logger.info(
                f"{log_prefix} → ==================================================\n"
                f"{log_prefix} → ORIGINAL CAPTION\n"
                f"{log_prefix} → ==================================================\n"
                f"{log_prefix} → {caption}\n"
                f"{log_prefix} → =================================================="
            )

            # Log complete Gemini raw response (no truncation)
            logger.info(
                f"{log_prefix} → ==================================================\n"
                f"{log_prefix} → GEMINI RAW RESPONSE\n"
                f"{log_prefix} → ==================================================\n"
                f"{log_prefix} → Model: {model_name}\n"
                f"{log_prefix} → Finish Reason: {metadata['finish_reason']}\n"
                f"{log_prefix} → Prompt Tokens: {metadata['tokens_in']}\n"
                f"{log_prefix} → Candidates Tokens: {metadata['tokens_out']}\n"
                f"{log_prefix} → Total Tokens: {metadata['total_tokens']}\n"
                f"{log_prefix} → \n"
                f"{log_prefix} → FULL RESPONSE:\n"
                f"{log_prefix} → {rewritten}\n"
                f"{log_prefix} → =================================================="
            )

            # Validate that rewritten caption contains essential elements
            # Check if it's significantly shorter than original (possible truncation)
            validation_fail_reason = None
            if not rewritten or rewritten.strip() == "":
                validation_fail_reason = "Empty response"
            elif len(rewritten) < len(caption) * 0.5:
                validation_fail_reason = f"Too short ({len(rewritten)} vs {len(caption)} chars)"
            else:
                # Check if essential elements are present (price, URL patterns)
                has_price = any(char.isdigit() for char in rewritten)  # Simple check for numbers (price)
                has_url = "http" in rewritten or "amazon" in rewritten.lower()
                if not has_price:
                    validation_fail_reason = "Missing price"
                elif not has_url:
                    validation_fail_reason = "Missing URL"

            if validation_fail_reason:
                logger.warning(
                    f"{log_prefix} → AI REWRITE VALIDATION FAILED: {validation_fail_reason}. "
                    f"Falling back to original caption."
                )
                logger.info(
                    f"{log_prefix} → ==================================================\n"
                    f"{log_prefix} → FINAL CAPTION (fallback to original)\n"
                    f"{log_prefix} → ==================================================\n"
                    f"{log_prefix} → {caption}\n"
                    f"{log_prefix} → =================================================="
                )
                key_pool.report_failure(key, f"Validation failed: {validation_fail_reason}")
                logger.info(f"{log_prefix} → AI REWRITE FAILED: validation={validation_fail_reason}")
                return caption

            # Report success
            key_pool.report_success(key)

            # Log success
            logger.info(
                f"{log_prefix} → AI REWRITE SUCCESS: model=%s duration_ms=%s tokens_in=%s tokens_out=%s",
                model_name,
                duration_ms,
                metadata['tokens_in'],
                metadata['tokens_out'],
            )

            # Log final caption (complete, no truncation)
            logger.info(
                f"{log_prefix} → ==================================================\n"
                f"{log_prefix} → FINAL CAPTION (rewritten)\n"
                f"{log_prefix} → ==================================================\n"
                f"{log_prefix} → {rewritten}\n"
                f"{log_prefix} → =================================================="
            )

            # Cache the result (unless skip_cache is True)
            if not skip_cache:
                db.set_gemini_rewrite_cache(caption, rewritten)

            return rewritten

        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            duration_ms = int((time.time() - start_time) * 1000)
            
            # Check if this is a 429 rate limit error
            is_429 = "429" in error_str or "quota" in error_str or "rate limit" in error_str
            
            if is_429:
                # Extract retry delay if available
                retry_delay = key_pool.get_retry_delay_from_error(e)
                if retry_delay is None:
                    retry_delay = 60.0  # Default 60 seconds
                
                key_pool.put_on_cooldown(key, retry_delay)
                logger.warning(
                    f"{log_prefix} → GEMINI KEY #{key.index} RATE LIMITED (429). "
                    f"Cooldown: {retry_delay}s. Switching to next key."
                )
                continue
            else:
                # For other errors, mark as failure but don't cooldown
                # This allows temporary network errors to retry with same key
                key_pool.report_failure(key, str(e))
                logger.warning(
                    f"{log_prefix} → GEMINI KEY #{key.index} ERROR: {str(e)}. "
                    f"Trying next key."
                )
                continue

    # All keys failed
    logger.error(
        f"{log_prefix} → ALL GEMINI KEYS FAILED. Attempted keys: {keys_attempted}. "
        f"Last error: {str(last_error) if last_error else 'No keys available'}. "
        f"Falling back to original caption."
    )
    return caption
