# Kaspi Profit Bot v4.19.0

- Separate cost resolution for products even when shop SKU/articles coincide across hat, stole or other categories.
- Model cost keyed by normalized title + product type; repeated colors can share one price after Telegram confirmation.
- Per-product overrides take priority; ambiguous legacy SKU prices cannot contaminate reports.
- Quantity-aware profit and refund accounting continue unchanged from 4.18.
- Missing-cost queue and Excel export distinguish product identities; legacy SKU-only Excel imports refuse ambiguous articles.
- No automatic deletion or overwrite of the SQLite database or historical SKU cost table.
- Publishing source code without credentials; secrets stay in Railway Variables and volume.
