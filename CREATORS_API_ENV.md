# Amazon Creators API — Environment Variables

The bot uses the **Creators API as the primary product data source**. Playwright remains
available for coupon detection, screenshots, and full fallback when the API is unavailable.

## Required (Creators API mode)

| Variable | Description | Example |
|----------|-------------|---------|
| `CREATORS_CREDENTIAL_ID` | OAuth client ID from Associates Creators API portal | `amzn1.application-oa2-client.xxx` |
| `CREATORS_CREDENTIAL_SECRET` | OAuth client secret | *(secret)* |
| `CREATORS_CREDENTIAL_VERSION` | Credential version (determines token endpoint) | `2.2` (EU / Egypt) |
| `CREATORS_PARTNER_TAG` | Affiliate partner tag | `yourtag-21` |
| `CREATORS_MARKETPLACE` | Marketplace host header + request body | `www.amazon.eg` |

## Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `CREATORS_API_TPS` | `1` | Max requests per second (global limiter) |
| `CREATORS_API_TPD` | `8640` | Max requests per UTC day |
| `CREATORS_TOKEN_REFRESH_MARGIN_SEC` | `300` | Refresh token N seconds before expiry |
| `FRAME_PRODUCT_IMAGES` | `true` | Apply `frame.png` overlay to product images |
| `AMAZON_DOMAIN` | `www.amazon.eg` | Used when `CREATORS_MARKETPLACE` is unset |

## Credential version → token endpoint

| Version | Region | Token endpoint |
|---------|--------|----------------|
| `2.1` | NA | `creatorsapi.auth.us-east-1.amazoncognito.com` |
| `2.2` | EU | `creatorsapi.auth.eu-south-2.amazoncognito.com` |
| `2.3` | FE | `creatorsapi.auth.us-west-2.amazoncognito.com` |
| `3.x` | LWA | `api.amazon.com/auth/o2/token` |

## Behavior without credentials

If Creators variables are missing, the bot **automatically falls back to Playwright**
for all product data. No configuration change is required for legacy operation.

## Affiliate URLs

- **Creators API** `detailPageURL` values are used **exactly as returned** — no tag
  replacement, shortening, or query changes.
- **Playwright fallback** continues to use `affiliate_tag.py` and existing dashboard settings.

## Cache (SQLite `creators_cache`)

| Profile | TTL |
|---------|-----|
| `draft` | 1 hour |
| `price_drop` | 1 hour |
| `search` | 24 hours |
| `features` | 24 hours |
