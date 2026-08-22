"""
Comprehensive test suite verifying Scenarios A through J for:
- Structured product resolver (resolve_product_input)
- Resale and NEW seller classification & merchant ID preservation across direct & short links
- Draft creation, publishing, monitoring, and republishing lifecycle
- Strict seller isolation & zero fallback rules
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, r"e:/BOTS_WEBSITES/Amazon_bot")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from creators_api import AMAZON_RESALE_SELLER_ID, NEW_AMAZON_SELLER_ID, extract_seller_offer
from database import Database
from link_resolver import (
    ResolvedProductInput,
    build_clean_url,
    classify_seller_from_merchant_id,
    extract_merchant_id,
    resolve_product_input,
)
from manual_posts import prepare_draft_from_input
from price_monitoring import evaluate_product_price_check, republish_published_product
from product_fetcher import fetch_product


class TestResaleResolverLifecycle(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db = Database(self.tmp_db.name)
        self.db.set_setting("destination_channel_id", "-100123456789")
        self.db.set_setting("coupon_detection_enabled", "0")
        self.db.add_destination("Test Dest", -100123456789)

    def tearDown(self):
        try:
            os.remove(self.tmp_db.name)
        except Exception:
            pass

    # --- Scenario A: Direct Resale Amazon URL ---
    async def test_scenario_a_direct_resale_url(self):
        url = f"https://www.amazon.eg/dp/B0BHJLYFXS?m={AMAZON_RESALE_SELLER_ID}"
        res = await resolve_product_input(url)
        self.assertIsNotNone(res)
        self.assertEqual(res.asin, "B0BHJLYFXS")
        self.assertEqual(res.seller_type, "AMAZON_RESALE")
        self.assertEqual(res.merchant_id, AMAZON_RESALE_SELLER_ID)
        self.assertIn(f"m={AMAZON_RESALE_SELLER_ID}", res.clean_url)

    # --- Scenario B: Shortened URL redirecting to Resale ---
    async def test_scenario_b_shortened_url_redirecting_to_resale(self):
        short_url = "https://amzn.to/3shortResale"
        dest_url = f"https://www.amazon.eg/dp/B0BHJLYFXS?m={AMAZON_RESALE_SELLER_ID}"
        with patch("link_resolver.resolve_redirect", new_callable=AsyncMock) as mock_redirect:
            mock_redirect.return_value = dest_url
            res = await resolve_product_input(short_url)
            self.assertIsNotNone(res)
            self.assertEqual(res.asin, "B0BHJLYFXS")
            self.assertEqual(res.seller_type, "AMAZON_RESALE")
            self.assertEqual(res.merchant_id, AMAZON_RESALE_SELLER_ID)

    # --- Scenario C: Direct NEW Amazon URL ---
    async def test_scenario_c_direct_new_amazon_url(self):
        url = f"https://www.amazon.eg/dp/B0BHJLYFXS?m={NEW_AMAZON_SELLER_ID}"
        res = await resolve_product_input(url)
        self.assertIsNotNone(res)
        self.assertEqual(res.asin, "B0BHJLYFXS")
        self.assertEqual(res.seller_type, "NEW_AMAZON")
        self.assertEqual(res.merchant_id, NEW_AMAZON_SELLER_ID)

    # --- Scenario D: Shortened URL redirecting to NEW Amazon ---
    async def test_scenario_d_shortened_url_redirecting_to_new_amazon(self):
        short_url = "https://amzn.to/3shortNew"
        dest_url = f"https://www.amazon.eg/dp/B0BHJLYFXS?m={NEW_AMAZON_SELLER_ID}"
        with patch("link_resolver.resolve_redirect", new_callable=AsyncMock) as mock_redirect:
            mock_redirect.return_value = dest_url
            res = await resolve_product_input(short_url)
            self.assertIsNotNone(res)
            self.assertEqual(res.asin, "B0BHJLYFXS")
            self.assertEqual(res.seller_type, "NEW_AMAZON")
            self.assertEqual(res.merchant_id, NEW_AMAZON_SELLER_ID)

    # --- Scenario E: Resale short URL clean_url preserves m=A2N2MP47XAP1MK ---
    async def test_scenario_e_resale_short_url_preserves_merchant(self):
        short_url = "https://amzn.to/3resalePreserved"
        dest_url = f"https://www.amazon.eg/dp/B0BHJLYFXS?m={AMAZON_RESALE_SELLER_ID}&ref=assoc_tag"
        with patch("link_resolver.resolve_redirect", new_callable=AsyncMock) as mock_redirect:
            mock_redirect.return_value = dest_url
            res = await resolve_product_input(short_url)
            self.assertIsNotNone(res)
            self.assertEqual(res.clean_url, f"https://www.amazon.eg/dp/B0BHJLYFXS?m={AMAZON_RESALE_SELLER_ID}")

    # --- Scenario F: NEW short URL clean_url preservation ---
    async def test_scenario_f_new_short_url_clean_url(self):
        short_url = "https://amzn.to/3newPreserved"
        dest_url = f"https://www.amazon.eg/dp/B0BHJLYFXS?m={NEW_AMAZON_SELLER_ID}&ref=assoc_tag"
        with patch("link_resolver.resolve_redirect", new_callable=AsyncMock) as mock_redirect:
            mock_redirect.return_value = dest_url
            res = await resolve_product_input(short_url)
            self.assertIsNotNone(res)
            self.assertEqual(res.clean_url, f"https://www.amazon.eg/dp/B0BHJLYFXS?m={NEW_AMAZON_SELLER_ID}")

    # --- Scenario G: Resale publish from short link ---
    async def test_scenario_g_resale_publish_from_short_link(self):
        short_url = "https://amzn.to/3publishResaleShort"
        dest_url = f"https://www.amazon.eg/dp/B0BHJLYFXS?m={AMAZON_RESALE_SELLER_ID}"

        mock_item = MagicMock()
        mock_item.title = "Resale Item"
        mock_item.image_url = "http://example.com/img.jpg"
        mock_item.list_price = "1000.00 EGP"
        mock_item.prime_exclusive = False
        mock_item.detail_page_url = dest_url
        mock_item.raw_listings = [
            {
                "merchantInfo": {"id": AMAZON_RESALE_SELLER_ID, "name": "Amazon Resale"},
                "condition": {"displayValue": "Used - Like New"},
                "price": {"money": {"amount": 700.0, "currency": "EGP", "displayAmount": "700.00 EGP"}},
            }
        ]

        mock_app = MagicMock()
        mock_app.bot_data = {"db": self.db, "browser": None}

        with patch("link_resolver.resolve_redirect", new_callable=AsyncMock, return_value=dest_url), \
             patch("product_fetcher.get_creators_client") as mock_get_client, \
             patch("product_fetcher.creators_api_configured", return_value=True), \
             patch("product_fetcher._download_best_amazon_image", new_callable=AsyncMock, return_value=True), \
             patch("product_fetcher.apply_frame_creators_product", return_value="dummy.png"), \
             patch("product_fetcher._require_screenshot", return_value="dummy.png"), \
             patch("os.path.exists", return_value=True):
            
            mock_client = AsyncMock()
            mock_client.get_items.return_value = {"B0BHJLYFXS": mock_item}
            mock_get_client.return_value = mock_client

            draft, img = await prepare_draft_from_input(mock_app, admin_id=123, item=short_url, scrape_key="key1")
            self.assertIsNotNone(draft)
            self.assertEqual(draft["seller_type"], "AMAZON_RESALE")
            self.assertIn(f"m={AMAZON_RESALE_SELLER_ID}", draft["clean_url"])

    # --- Scenario H: Resale republish from stored record ---
    async def test_scenario_h_resale_republish_from_stored_record(self):
        pub_id = self.db.add_published_product(
            asin="B0BHJLYFXS",
            title="Stored Resale Product",
            source_channel_id=123,
            destination_message_id=456,
            destination_id=1,
            seller_type="AMAZON_RESALE",
            image_path="stored.png",
            published_price="700.00 EGP",
            published_price_value=700.0,
            published_currency="EGP",
        )

        mock_item = MagicMock()
        mock_item.title = "Stored Resale Product"
        mock_item.image_url = "http://example.com/img.jpg"
        mock_item.list_price = "1000.00 EGP"
        mock_item.raw_listings = [
            {
                "merchantInfo": {"id": AMAZON_RESALE_SELLER_ID, "name": "Amazon Resale"},
                "condition": {"displayValue": "Used - Like New"},
                "price": {"money": {"amount": 650.0, "currency": "EGP", "displayAmount": "650.00 EGP"}},
            }
        ]

        mock_app = MagicMock()
        mock_app.bot_data = {"db": self.db, "browser": None, "destination_channel_id": -100123456789}

        with patch("price_monitoring.get_creators_client") as mock_get_client, \
             patch("price_monitoring.creators_api_configured", return_value=True), \
             patch("price_monitoring._download_best_amazon_image", new_callable=AsyncMock, return_value=True), \
             patch("price_monitoring.apply_frame_creators_product", return_value="dummy_repub.png"), \
             patch("price_monitoring._require_screenshot", return_value="dummy_repub.png"), \
             patch("price_monitoring.to_jpeg_for_telegram", return_value="dummy_repub.png"), \
             patch("os.path.exists", return_value=True), \
             patch("price_monitoring.publish_to_destinations", new_callable=AsyncMock) as mock_pub:
            
            mock_client = AsyncMock()
            mock_client.get_items.return_value = {"B0BHJLYFXS": mock_item}
            mock_get_client.return_value = mock_client

            mock_res = MagicMock()
            mock_res.successful = 1
            mock_res.total = 1
            s_res = MagicMock()
            s_res.success = True
            s_res.message_id = 789
            s_res.destination_id = 1
            mock_res.results = [s_res]
            mock_pub.return_value = mock_res

            res_msg = await republish_published_product(mock_app, pub_id)
            self.assertIn("Republished ASIN", res_msg)

            updated = self.db.get_published_product(pub_id)
            self.assertEqual(updated["seller_type"], "AMAZON_RESALE")
            self.assertEqual(updated["published_price_value"], 650.0)

    # --- Scenario I: Resale unavailable + NEW available -> Abort Resale, no fallback ---
    async def test_scenario_i_resale_unavailable_aborts_without_fallback(self):
        pub_id = self.db.add_published_product(
            asin="B0BHJLYFXS",
            title="Stored Resale Product",
            source_channel_id=123,
            destination_message_id=456,
            destination_id=1,
            seller_type="AMAZON_RESALE",
            image_path="stored.png",
            published_price="700.00 EGP",
            published_price_value=700.0,
            published_currency="EGP",
        )

        # Only NEW_AMAZON offer is returned; Resale offer is missing
        mock_item_new_only = MagicMock()
        mock_item_new_only.asin = "B0BHJLYFXS"
        mock_item_new_only.title = "Stored Resale Product"
        mock_item_new_only.raw_listings = [
            {
                "merchantInfo": {"id": NEW_AMAZON_SELLER_ID, "name": "Amazon.eg"},
                "condition": {"displayValue": "New"},
                "price": {"money": {"amount": 900.0, "currency": "EGP", "displayAmount": "900.00 EGP"}},
            }
        ]

        mock_app = MagicMock()
        mock_app.bot_data = {"db": self.db, "browser": None, "destination_channel_id": -100123456789}

        with patch("price_monitoring.get_creators_client") as mock_get_client, \
             patch("price_monitoring.creators_api_configured", return_value=True):
            
            mock_client = AsyncMock()
            mock_client.get_items.return_value = {"B0BHJLYFXS": mock_item_new_only}
            mock_get_client.return_value = mock_client

            res_msg = await republish_published_product(mock_app, pub_id)
            self.assertIn("Amazon Resale is currently unavailable", res_msg)

    # --- Scenario J: Missing merchant parameter -> NEW_AMAZON, no silent conversion to Resale ---
    async def test_scenario_j_missing_merchant_defaults_to_new(self):
        url = "https://www.amazon.eg/dp/B0BHJLYFXS"
        res = await resolve_product_input(url)
        self.assertIsNotNone(res)
        self.assertEqual(res.asin, "B0BHJLYFXS")
        self.assertEqual(res.seller_type, "NEW_AMAZON")
        self.assertIsNone(res.merchant_id)
        self.assertEqual(res.clean_url, "https://www.amazon.eg/dp/B0BHJLYFXS")


if __name__ == "__main__":
    unittest.main()
