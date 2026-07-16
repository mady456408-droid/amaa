import logging
import os
import time
from typing import Any

import google.generativeai as genai

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


def rewrite_caption(caption: str, db: Any, skip_cache: bool = False, log_prefix: str = "") -> str:
    """
    Rewrite caption using Gemini AI.

    Args:
        caption: Original caption to rewrite
        db: Database instance to read settings from
        skip_cache: If True, bypass cache and call Gemini directly
        log_prefix: Prefix for log messages (e.g., "MANUAL POST", "SOURCE POST")

    Returns:
        Rewritten caption, or original caption if Gemini is disabled or fails
    """
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
        logger.info(f"{log_prefix} → CACHE MISS: calling Gemini")

    # Read settings from database
    model_name = db.get_gemini_model()
    admin_prompt = db.get_gemini_system_prompt()
    temperature = db.get_gemini_temperature()
    max_tokens = db.get_gemini_max_tokens()

    # Read API key from environment
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.warning(f"{log_prefix} → AI REWRITE FAILED: GEMINI_API_KEY not set")
        return caption

    logger.info(
        f"{log_prefix} → AI REWRITE START: model=%s temperature=%s max_tokens=%s",
        model_name,
        temperature,
        max_tokens,
    )

    start_time = time.time()

    try:
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

        duration_ms = int((time.time() - start_time) * 1000)

        # Get token counts and finish reason
        tokens_in = response.usage_metadata.prompt_token_count if hasattr(response, "usage_metadata") else "N/A"
        tokens_out = response.usage_metadata.candidates_token_count if hasattr(response, "usage_metadata") else "N/A"
        total_tokens = response.usage_metadata.total_token_count if hasattr(response, "usage_metadata") else "N/A"
        finish_reason = response.candidates[0].finish_reason if hasattr(response, "candidates") and response.candidates else "N/A"

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
            f"{log_prefix} → Finish Reason: {finish_reason}\n"
            f"{log_prefix} → Prompt Tokens: {tokens_in}\n"
            f"{log_prefix} → Candidates Tokens: {tokens_out}\n"
            f"{log_prefix} → Total Tokens: {total_tokens}\n"
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
            return caption

        # Log success
        logger.info(
            f"{log_prefix} → AI REWRITE SUCCESS: model=%s duration_ms=%s tokens_in=%s tokens_out=%s",
            model_name,
            duration_ms,
            tokens_in,
            tokens_out,
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
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error(
            f"{log_prefix} → AI REWRITE FAILED: model=%s duration_ms=%s error=%s",
            model_name,
            duration_ms,
            str(e),
        )
        # Fallback to original caption
        return caption
