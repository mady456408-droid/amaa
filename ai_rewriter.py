"""AI Caption Rewriter Dispatcher.

Central entry point for AI caption rewriting.
Dispatches to the appropriate provider (Gemini or ChatGPT) based on database settings.

This is the only module that knows about both providers.
Each provider module is completely independent and unaware of the other.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def rewrite_caption(caption: str, db: Any, skip_cache: bool = False, log_prefix: str = "") -> str:
    """
    Rewrite caption using selected AI provider (Gemini or ChatGPT).

    This is the main entry point for AI caption rewriting. It dispatches to the
    appropriate provider based on the ai_provider setting.

    Args:
        caption: Original caption to rewrite
        db: Database instance to read settings from
        skip_cache: If True, bypass cache and call API directly
        log_prefix: Prefix for log messages (e.g., "MANUAL POST", "SOURCE POST")

    Returns:
        Rewritten caption, or original caption if AI is disabled or all providers fail
    """
    logger.info(f"{log_prefix} → ENTERING AI REWRITE")

    # Get selected provider
    provider = db.get_ai_provider()
    logger.info(f"{log_prefix} → AI PROVIDER = {provider.upper()}")

    # Dispatch to appropriate provider
    if provider == "gemini":
        # Check if Gemini is enabled
        if not db.get_gemini_enabled():
            logger.info(f"{log_prefix} → GEMINI REWRITE SKIPPED: Gemini disabled")
            return caption
        
        logger.info(f"{log_prefix} → DISPATCHING TO gemini_rewriter")
        from gemini_rewriter import rewrite_caption as gemini_rewrite
        return gemini_rewrite(caption, db, skip_cache=skip_cache, log_prefix=log_prefix)
    elif provider == "chatgpt":
        # Check if ChatGPT rewrite is enabled
        if not db.get_chatgpt_rewrite_enabled():
            logger.info(f"{log_prefix} → CHATGPT REWRITE SKIPPED: ChatGPT disabled")
            return caption
        
        # Apply ChatGPT skip_cache setting if not already overridden
        if not skip_cache and db.get_chatgpt_skip_cache():
            skip_cache = True
        
        logger.info(f"{log_prefix} → DISPATCHING TO chatgpt_rewriter")
        from chatgpt_rewriter import rewrite_caption as chatgpt_rewrite
        return chatgpt_rewrite(caption, db, skip_cache=skip_cache, log_prefix=log_prefix)
    else:
        logger.warning(f"{log_prefix} → Unknown provider: {provider}. Falling back to original caption.")
        return caption
