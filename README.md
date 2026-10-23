# Kaspi Profit Bot v4.18.0

Isolated deployment branch for the Kaspi Telegram profit analytics bot. The default `main` branch belongs to another app and is not changed.

Features: correct per-unit quantity and costing, returns with stock/commission restoration and delivery/ad losses, Marketing Playwright browser compatibility.

Railway: deploy this branch to `kaspi-profit-bot`, persistent volume `/app/data`, variables via Railway UI. Never commit .env, Telegram bot token, merchant API credentials, database or private data.
