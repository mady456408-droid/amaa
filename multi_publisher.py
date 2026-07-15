"""Multi-destination publisher wrapper for Telegram bot."""

import logging
from typing import Any

from telegram import Bot, Message
from telegram_publisher import publish_to_channel_with_overflow

logger = logging.getLogger(__name__)


class PublishResult:
    """Result of publishing to a single destination."""
    def __init__(
        self,
        destination_id: int,
        title: str,
        chat_id: int,
        success: bool,
        message_id: int | None = None,
        error: str | None = None,
    ):
        self.destination_id = destination_id
        self.title = title
        self.chat_id = chat_id
        self.success = success
        self.message_id = message_id
        self.error = error


class MultiPublishResult:
    """Result of publishing to multiple destinations."""
    def __init__(self, results: list[PublishResult]):
        self.results = results
        self.total = len(results)
        self.successful = sum(1 for r in results if r.success)
        self.failed = self.total - self.successful

    def log_summary(self) -> None:
        """Log a summary of the multi-destination publish operation."""
        logger.info(
            "MULTI DESTINATION destinations=%s published=%s failed=%s",
            self.total,
            self.successful,
            self.failed,
        )
        if self.failed > 0:
            logger.info("details:")
            for result in self.results:
                if result.success:
                    logger.info("✓ %s", result.title)
                else:
                    logger.info("✗ %s (%s)", result.title, result.error)


async def publish_to_destinations(
    bot: Bot,
    destinations: list[dict[str, Any]],
    image_path: str,
    caption: str,
    *,
    reply_markup: Any = None,
    products: list[dict[str, str]] | None = None,
    parse_mode: str | None = None,
) -> MultiPublishResult:
    """Publish the same content to multiple enabled destinations.

    Args:
        bot: Telegram bot instance
        destinations: List of destination dicts from database
        image_path: Path to the image to publish
        caption: Caption text
        reply_markup: Inline keyboard markup
        products: List of product dicts for inline buttons
        parse_mode: Parse mode for caption

    Returns:
        MultiPublishResult with results for each destination
    """
    logger.info("publish_to_destinations: Starting with %s destinations", len(destinations))
    for i, dest in enumerate(destinations):
        logger.info("  [%s] destination_id=%s title=%s chat_id=%s enabled=%s",
                   i, dest.get("id"), dest.get("title"), dest.get("chat_id"), dest.get("enabled"))

    results: list[PublishResult] = []

    for destination in destinations:
        try:
            logger.info(
                "PUBLISH START destination_id=%s title=%s chat_id=%s",
                destination["id"],
                destination["title"],
                destination["chat_id"],
            )

            sent = await publish_to_channel_with_overflow(
                bot,
                destination["chat_id"],
                image_path,
                caption,
                reply_markup=reply_markup,
                products=products,
                parse_mode=parse_mode,
            )

            results.append(
                PublishResult(
                    destination_id=destination["id"],
                    title=destination["title"],
                    chat_id=destination["chat_id"],
                    success=True,
                    message_id=sent.message_id,
                )
            )
            logger.info(
                "PUBLISH SUCCESS destination_id=%s title=%s chat_id=%s message_id=%s",
                destination["id"],
                destination["title"],
                destination["chat_id"],
                sent.message_id,
            )
        except Exception as e:
            error_msg = str(e)
            logger.exception(
                "PUBLISH FAILURE destination_id=%s title=%s chat_id=%s error=%s",
                destination["id"],
                destination["title"],
                destination["chat_id"],
                error_msg,
            )
            results.append(
                PublishResult(
                    destination_id=destination["id"],
                    title=destination["title"],
                    chat_id=destination["chat_id"],
                    success=False,
                    error=error_msg,
                )
            )

    logger.info("publish_to_destinations: Completed. Total=%s Successful=%s Failed=%s",
               len(results), sum(1 for r in results if r.success), sum(1 for r in results if not r.success))
    return MultiPublishResult(results)
