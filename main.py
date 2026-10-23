import asyncio
import calendar
import csv
import html
import hashlib
import io
import json
import os
import re
import secrets
import socket
import sqlite3
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import httpx
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from PIL import Image as PILImage
from playwright.async_api import async_playwright
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut, Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "kaspi.db"
KEY_PATH = DATA_DIR / "secret.key"
MARKETING_DIR = DATA_DIR / "marketing"
MARKETING_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_TELEGRAM_USER_ID = os.getenv("ALLOWED_TELEGRAM_USER_ID", "").strip()
PUBLIC_ACCESS = (
    os.getenv("PUBLIC_ACCESS", "true").strip().lower()
    not in {"0", "false", "no", "off"}
)
MAX_SHOPS_PER_USER = max(
    1,
    int(
        os.getenv(
            "MAX_SHOPS_PER_USER",
            os.getenv("MAX_SHOPS", "5"),
        )
    ),
)
TIMEZONE = os.getenv("TIMEZONE", "Asia/Almaty").strip()
SYNC_INTERVAL_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "10"))
ROLLING_SYNC_DAYS = int(os.getenv("ROLLING_SYNC_DAYS", "90"))
MARKETING_SYNC_INTERVAL_MINUTES = int(
    os.getenv("MARKETING_SYNC_INTERVAL_MINUTES", "30")
)
BUSINESS_DAY_START_HOUR = int(
    os.getenv("BUSINESS_DAY_START_HOUR", "11")
)
TELEGRAM_BOOTSTRAP_RETRIES = int(
    os.getenv("TELEGRAM_BOOTSTRAP_RETRIES", "20")
)

KASPI_MARKETING_SIGN_IN_URL = os.getenv(
    "KASPI_MARKETING_SIGN_IN_URL",
    "https://marketing.kaspi.kz/sign-in",
).strip()

KASPI_MARKETING_HOME_URL = os.getenv(
    "KASPI_MARKETING_HOME_URL",
    "https://marketing.kaspi.kz/",
).strip()
MARKETING_HEADLESS = (
    os.getenv("MARKETING_HEADLESS", "true").strip().lower()
    not in {"0", "false", "no"}
)
MARKETING_BROWSER_TIMEOUT_MS = int(
    os.getenv("MARKETING_BROWSER_TIMEOUT_MS", "45000")
)

MARKETING_INITIAL_LOOKBACK_DAYS = int(
    os.getenv("MARKETING_INITIAL_LOOKBACK_DAYS", "7")
)
MARKETING_REFRESH_DAYS = int(
    os.getenv("MARKETING_REFRESH_DAYS", "7")
)
MARKETING_RECENT_ACTIVITY_DAYS = int(
    os.getenv("MARKETING_RECENT_ACTIVITY_DAYS", "7")
)
MARKETING_SYNC_CONCURRENCY = max(
    1,
    min(
        12,
        int(
            os.getenv(
                "MARKETING_SYNC_CONCURRENCY",
                "4",
            )
        ),
    ),
)

MARKETING_PREFLIGHT_CAMPAIGNS = max(
    1,
    min(
        3,
        int(
            os.getenv(
                "MARKETING_PREFLIGHT_CAMPAIGNS",
                "2",
            )
        ),
    ),
)


TZ = ZoneInfo(TIMEZONE)
KASPI_BASE = "https://kaspi.kz/shop/api/v2"

ACTIVE_EXPECTED = {"APPROVED_BY_BANK", "ACCEPTED_BY_MERCHANT"}
COMPLETED = {"COMPLETED"}
CANCELLED = {"CANCELLED"}
CANCELLING = {"CANCELLING"}
RETURN_REQUESTED = {"KASPI_DELIVERY_RETURN_REQUESTED"}
RETURNED = {"RETURNED"}

ORDER_STATES = (
    "NEW",
    "SIGN_REQUIRED",
    "PICKUP",
    "DELIVERY",
    "KASPI_DELIVERY",
    "ARCHIVE",
)

MONTHS = {
    "января": 1, "январь": 1,
    "февраля": 2, "февраль": 2,
    "марта": 3, "март": 3,
    "апреля": 4, "апрель": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июнь": 6,
    "июля": 7, "июль": 7,
    "августа": 8, "август": 8,
    "сентября": 9, "сентябрь": 9,
    "октября": 10, "октябрь": 10,
    "ноября": 11, "ноябрь": 11,
    "декабря": 12, "декабрь": 12,
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    token_encrypted TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shops_user ON shops(telegram_user_id);

CREATE TABLE IF NOT EXISTS orders (
    shop_id INTEGER NOT NULL,
    id TEXT NOT NULL,
    code TEXT,
    total_price REAL NOT NULL DEFAULT 0,
    status TEXT,
    state TEXT,
    creation_ms INTEGER,
    completion_ms INTEGER,
    delivery_cost_for_seller REAL NOT NULL DEFAULT 0,
    delivery_cost_for_seller_known INTEGER NOT NULL DEFAULT 0,
    delivery_cost REAL NOT NULL DEFAULT 0,
    is_kaspi_delivery INTEGER,
    delivery_mode TEXT,
    returned_to_warehouse INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(shop_id, id),
    FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_orders_shop_creation ON orders(shop_id, creation_ms);
CREATE INDEX IF NOT EXISTS idx_orders_shop_completion ON orders(shop_id, completion_ms);
CREATE INDEX IF NOT EXISTS idx_orders_shop_status ON orders(shop_id, status);

CREATE TABLE IF NOT EXISTS order_items (
    shop_id INTEGER NOT NULL,
    entry_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 0,
    total_price REAL NOT NULL DEFAULT 0,
    base_price REAL NOT NULL DEFAULT 0,
    delivery_cost REAL NOT NULL DEFAULT 0,
    master_product_id TEXT,
    kaspi_product_code TEXT,
    sku TEXT,
    name TEXT,
    category TEXT,
    image_url TEXT,
    raw_json TEXT,
    PRIMARY KEY(shop_id, entry_id),
    FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_items_shop_order ON order_items(shop_id, order_id);
CREATE INDEX IF NOT EXISTS idx_items_shop_sku ON order_items(shop_id, sku);

CREATE TABLE IF NOT EXISTS costs (
    shop_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    cost_price REAL NOT NULL,
    commission_rate REAL NOT NULL DEFAULT 0,
    tax_rate REAL NOT NULL DEFAULT 0,
    acquiring_rate REAL NOT NULL DEFAULT 0,
    packaging_cost REAL NOT NULL DEFAULT 0,
    other_cost REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(shop_id, sku),
    FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS shop_financial_settings (
    shop_id INTEGER PRIMARY KEY,
    default_commission_rate REAL NOT NULL DEFAULT 15.5,
    default_tax_rate REAL NOT NULL DEFAULT 3.0,
    default_acquiring_rate REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS model_costs (
    shop_id INTEGER NOT NULL,
    group_key TEXT NOT NULL,
    display_name TEXT,
    cost_price REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(shop_id, group_key),
    FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_model_costs_shop
ON model_costs(shop_id);

CREATE TABLE IF NOT EXISTS product_cost_overrides (
    shop_id INTEGER NOT NULL,
    identity_key TEXT NOT NULL,
    sku TEXT,
    category TEXT,
    name TEXT,
    cost_price REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(shop_id, identity_key),
    FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS cost_repair_backup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    cost_price REAL NOT NULL,
    commission_rate REAL NOT NULL DEFAULT 0,
    tax_rate REAL NOT NULL DEFAULT 0,
    packaging_cost REAL NOT NULL DEFAULT 0,
    other_cost REAL NOT NULL DEFAULT 0,
    original_updated_at TEXT,
    backup_reason TEXT,
    backed_up_at TEXT NOT NULL,
    restored_at TEXT,
    FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS marketing_accounts (
    shop_id INTEGER PRIMARY KEY,
    login_encrypted TEXT NOT NULL,
    password_encrypted TEXT NOT NULL,
    status TEXT,
    last_error TEXT,
    selected_cabinet_key TEXT,
    selected_cabinet_name TEXT,
    selected_merchant_id TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS marketing_campaign_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL,
    cabinet_key TEXT,
    cabinet_name TEXT,
    merchant_id TEXT,
    section_key TEXT NOT NULL,
    campaign_id TEXT,
    campaign_name TEXT NOT NULL,
    report_url TEXT,
    file_kind TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(shop_id, section_key, campaign_name),
    FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS marketing_report_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL,
    cabinet_key TEXT,
    cabinet_name TEXT,
    section_key TEXT NOT NULL,
    section_name TEXT NOT NULL,
    campaign_name TEXT,
    report_date TEXT,
    product_code TEXT,
    sku TEXT,
    product_name TEXT,
    order_code TEXT,
    amount REAL NOT NULL DEFAULT 0,
    clicks REAL,
    impressions REAL,
    orders_count REAL,
    revenue REAL,
    mapping_quality TEXT,
    source_file TEXT,
    fingerprint TEXT NOT NULL,
    sync_run_id INTEGER,
    raw_json TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    UNIQUE(shop_id, fingerprint),
    FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_marketing_rows_shop_date
ON marketing_report_rows(shop_id, report_date);

CREATE INDEX IF NOT EXISTS idx_marketing_rows_shop_sku
ON marketing_report_rows(shop_id, sku);

CREATE INDEX IF NOT EXISTS idx_marketing_rows_shop_order
ON marketing_report_rows(shop_id, order_code);

CREATE TABLE IF NOT EXISTS marketing_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL,
    cabinet_key TEXT,
    cabinet_name TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    files_downloaded INTEGER NOT NULL DEFAULT 0,
    rows_imported INTEGER NOT NULL DEFAULT 0,
    sections_seen TEXT,
    error TEXT,
    FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS order_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL,
    order_id TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    is_initial INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_history_shop_order
ON order_status_history(shop_id, order_id);


CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    from_ms INTEGER,
    to_ms INTEGER,
    orders_seen INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
);
"""


def get_fernet():
    if not KEY_PATH.exists():
        KEY_PATH.write_bytes(Fernet.generate_key())
    return Fernet(KEY_PATH.read_bytes())


FERNET = get_fernet()


def encrypt_token(token: str) -> str:
    return FERNET.encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_token(value: str) -> str:
    return FERNET.decrypt(value.encode("ascii")).decode("utf-8")


COLOR_PHRASES = (
    # Russian multi-word colors first
    "темно синий", "тёмно синий", "темно-синий", "тёмно-синий",
    "светло синий", "светло-синий",
    "темно серый", "тёмно серый", "темно-серый", "тёмно-серый",
    "светло серый", "светло-серый",
    "темно зеленый", "тёмно зелёный", "темно-зеленый", "тёмно-зелёный",
    "светло зеленый", "светло-зеленый",
    "пыльная роза", "кофе с молоком",
)

COLOR_WORDS = {
    # Russian
    "красный", "красная", "красное", "красные", "красного", "красной",
    "черный", "чёрный", "черная", "чёрная", "черное", "чёрное", "черные", "чёрные",
    "белый", "белая", "белое", "белые",
    "серый", "серая", "серое", "серые",
    "синий", "синяя", "синее", "синие",
    "голубой", "голубая", "голубое", "голубые",
    "зеленый", "зелёный", "зеленая", "зелёная", "зеленое", "зелёное", "зеленые", "зелёные",
    "розовый", "розовая", "розовое", "розовые",
    "фиолетовый", "фиолетовая", "фиолетовое", "фиолетовые",
    "сиреневый", "сиреневая", "сиреневое", "сиреневые",
    "бордовый", "бордовая", "бордовое", "бордовые", "бордо",
    "бежевый", "бежевая", "бежевое", "бежевые", "беж",
    "коричневый", "коричневая", "коричневое", "коричневые",
    "желтый", "жёлтый", "желтая", "жёлтая", "желтое", "жёлтое", "желтые", "жёлтые",
    "оранжевый", "оранжевая", "оранжевое", "оранжевые",
    "молочный", "молочная", "молочное", "молочные",
    "кремовый", "кремовая", "кремовое", "кремовые",
    "графит", "графитовый", "графитовая", "графитовые",
    "хаки",
    "карамельный", "карамельная", "карамельное", "карамельные", "карамель",
    "горчичный", "горчичная", "горчичное", "горчичные",
    "песочный", "песочная", "песочное", "песочные",
    "мятный", "мятная", "мятное", "мятные",
    "оливковый", "оливковая", "оливковое", "оливковые",
    "терракотовый", "терракотовая", "терракотовое", "терракотовые",
    "лавандовый", "лавандовая", "лавандовое", "лавандовые",
    "персиковый", "персиковая", "персиковое", "персиковые",
    "темный", "тёмный", "темная", "тёмная", "светлый", "светлая",
    # English
    "red", "black", "white", "grey", "gray", "blue", "navy", "green",
    "pink", "purple", "beige", "brown", "yellow", "orange", "cream",
    "khaki", "graphite", "burgundy", "olive", "mint", "lavender",
}


GENERIC_MODEL_WORDS = {
    "шапка", "шапки", "шапочка", "шапочки",
    "берет", "береты", "беретка", "беретки",
    "панама", "панамы", "кепка", "кепки",
    "картуз", "картузы", "капор", "капоры",
    "шарф", "шарфы", "палантин", "палантины",
    "снуд", "снуды", "перчатки", "варежки",
    "женская", "женский", "женское", "женские",
    "мужская", "мужской", "мужское", "мужские",
    "детская", "детский", "детское", "детские",
    "зимняя", "зимний", "зимнее", "зимние",
    "осенняя", "осенний", "осеннее", "осенние",
    "вязаная", "вязаный", "вязаное", "вязаные",
    "теплая", "теплый", "теплое", "теплые",
    "теплая", "теплый", "теплое", "теплые",
    "однотонная", "однотонный", "однотонное", "однотонные",
    "модель", "товар",
    "hat", "beanie", "cap", "scarf", "women", "woman",
    "men", "man", "winter", "knitted", "warm",
}

MAX_AUTO_MODEL_VARIANTS = 8


def legacy_model_cost_group_key(name, category=None):
    """Old v4.6 grouping key, used only to detect/repair unsafe old groups."""
    normalized = normalize_model_name(name)

    category_norm = re.sub(
        r"\s+",
        " ",
        str(category or "").strip().lower().replace("ё", "е"),
    )

    if not normalized:
        return None

    return f"{category_norm}::{normalized}"


def is_safe_normalized_model_name(normalized):
    if not normalized:
        return False

    tokens = [
        t for t in re.findall(r"[a-zа-я0-9-]+", normalized.lower())
        if t
    ]

    if len(tokens) < 2:
        return False

    distinctive = [
        token
        for token in tokens
        if token not in GENERIC_MODEL_WORDS
    ]

    # Need at least one genuinely distinguishing token such as a model name,
    # code or number. "женская шапка" is not enough.
    if not distinctive:
        return False

    return True


def normalize_model_name(name):
    """
    Turn e.g.:
      'Шапка Aurora красная' -> 'шапка aurora'
      'Шапка Aurora черная'  -> 'шапка aurora'

    This lets color variants inherit one cost.
    """
    value = str(name or "").strip().lower().replace("ё", "е")
    value = value.replace("_", " ").replace("/", " ")

    for phrase in COLOR_PHRASES:
        value = value.replace(phrase.replace("ё", "е"), " ")

    # punctuation -> spaces, but keep letters/numbers/hyphens as model identifiers
    value = re.sub(r"[()\[\]{},.;:|]+", " ", value)
    tokens = re.findall(r"[a-zа-я0-9-]+", value, flags=re.IGNORECASE)

    cleaned = []
    for token in tokens:
        token_norm = token.replace("ё", "е")
        if token_norm in {w.replace("ё", "е") for w in COLOR_WORDS}:
            continue
        cleaned.append(token_norm)

    return " ".join(cleaned).strip()




def cost_product_type(name, category=None):
    """Normalize type, taking specific item title before broad Kaspi category."""
    types = (
        ("палантин", ("палантин", "stole")),
        ("шапка", ("шапк", "шапоч", "beanie")),
        ("берет", ("берет",)),
        ("панама", ("панам", "bucket hat")),
        ("перчатки", ("перчат", "glove")),
        ("варежки", ("вареж", "mitten")),
        ("шарф", ("шарф", "scarf")),
        ("снуд", ("снуд",)),
        ("кепка", ("кепк", "бейсболк")),
        ("картуз", ("картуз",)),
    )
    title = str(name or "").lower().replace("ё", "е")
    fallback = str(category or "").lower().replace("ё", "е")
    for value in (title, fallback):
        for label, stems in types:
            if any(term in value for term in stems):
                return label
    return re.sub(r"\s+", " ", fallback).strip() or "неизвестная категория"


def product_cost_identity(item):
    """SKU is NOT a unique identifier. Include class and Kaspi product identity/name."""
    category = cost_product_type(item["name"], item["category"])
    unique_code = str(item["kaspi_product_code"] or item["master_product_id"] or item["sku"] or "").strip()
    name = re.sub(r"\s+", " ", str(item["name"] or "").lower().replace("ё", "е")).strip()
    return f"{category}::{unique_code}::{name}"

def model_cost_group_key(name, category=None):
    normalized = normalize_model_name(name)

    if not is_safe_normalized_model_name(normalized):
        return None

    return f"{cost_product_type(name, category)}::{normalized}"


def model_display_name(name):
    normalized = normalize_model_name(name)
    return normalized or str(name or "Товар").strip()


def money(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") + " ₸"


IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def extract_image_url(obj):
    """
    Extract only an actual Kaspi product-image CDN URL.
    Do not cache logos, banners or unrelated assets.
    """
    candidates = []

    def walk(value):
        if isinstance(value, dict):
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)
        elif isinstance(value, str):
            normalized = gallery_medium_url(value)
            if normalized:
                candidates.append(normalized)

    # gallery_medium_url is defined later at runtime, before this function
    # is actually called during synchronization.
    walk(obj)
    return candidates[0] if candidates else None


class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_schema()
        self._one_time_v48_cost_reset()
        self.conn.commit()

    def _one_time_v48_cost_reset(self):
        marker = "v4.8_costs_reset_done"

        row = self.conn.execute(
            "SELECT value FROM app_meta WHERE key=?",
            (marker,),
        ).fetchone()

        if row:
            return

        now = datetime.now(timezone.utc).isoformat()

        # Backup all existing exact SKU costs before clearing.
        rows = self.conn.execute(
            "SELECT * FROM costs"
        ).fetchall()

        for row in rows:
            self.conn.execute(
                """INSERT INTO cost_repair_backup
                   (shop_id,sku,cost_price,commission_rate,tax_rate,
                    packaging_cost,other_cost,original_updated_at,
                    backup_reason,backed_up_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["shop_id"],
                    row["sku"],
                    row["cost_price"],
                    row["commission_rate"],
                    row["tax_rate"],
                    row["packaging_cost"],
                    row["other_cost"],
                    row["updated_at"],
                    "v4.8_full_cost_reset",
                    now,
                ),
            )

        self.conn.execute("DELETE FROM costs")
        self.conn.execute("DELETE FROM model_costs")

        self.conn.execute(
            """INSERT INTO app_meta(key,value)
               VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (marker, now),
        )

        self.conn.commit()

        print(
            f"v4.8 one-time cost reset completed. "
            f"Cleared {len(rows)} SKU cost rows."
        )

    def _migrate_schema(self):
        # Existing user databases from v4.2 do not have image_url yet.
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(order_items)").fetchall()
        }
        if "image_url" not in columns:
            self.conn.execute(
                "ALTER TABLE order_items ADD COLUMN image_url TEXT"
            )

        cost_columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(costs)"
            ).fetchall()
        }
        if "acquiring_rate" not in cost_columns:
            self.conn.execute(
                "ALTER TABLE costs "
                "ADD COLUMN acquiring_rate REAL NOT NULL DEFAULT 0"
            )

        order_columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(orders)"
            ).fetchall()
        }
        if "delivery_cost_for_seller_known" not in order_columns:
            self.conn.execute(
                "ALTER TABLE orders ADD COLUMN "
                "delivery_cost_for_seller_known INTEGER NOT NULL DEFAULT 0"
            )
        if "delivery_cost" not in order_columns:
            self.conn.execute(
                "ALTER TABLE orders ADD COLUMN "
                "delivery_cost REAL NOT NULL DEFAULT 0"
            )
        if "is_kaspi_delivery" not in order_columns:
            self.conn.execute(
                "ALTER TABLE orders ADD COLUMN is_kaspi_delivery INTEGER"
            )
        if "delivery_mode" not in order_columns:
            self.conn.execute(
                "ALTER TABLE orders ADD COLUMN delivery_mode TEXT"
            )

        self._backfill_delivery_metadata()

        marketing_row_columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(marketing_report_rows)"
            ).fetchall()
        }
        if (
            marketing_row_columns
            and "sync_run_id" not in marketing_row_columns
        ):
            self.conn.execute(
                "ALTER TABLE marketing_report_rows "
                "ADD COLUMN sync_run_id INTEGER"
            )

        marketing_columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(marketing_accounts)"
            ).fetchall()
        }
        if marketing_columns:
            if "selected_cabinet_key" not in marketing_columns:
                self.conn.execute(
                    "ALTER TABLE marketing_accounts "
                    "ADD COLUMN selected_cabinet_key TEXT"
                )
            if "selected_cabinet_name" not in marketing_columns:
                self.conn.execute(
                    "ALTER TABLE marketing_accounts "
                    "ADD COLUMN selected_cabinet_name TEXT"
                )
            if "selected_merchant_id" not in marketing_columns:
                self.conn.execute(
                    "ALTER TABLE marketing_accounts "
                    "ADD COLUMN selected_merchant_id TEXT"
                )

            # Old builds could save a synthetic placeholder instead of a real
            # Marketing cabinet. Clear it so the user can select a real one.
            self.conn.execute(
                """UPDATE marketing_accounts
                   SET selected_cabinet_key=NULL,
                       selected_cabinet_name=NULL,
                       selected_merchant_id=NULL
                   WHERE selected_cabinet_name='Текущий кабинет Kaspi Marketing'
                      OR selected_cabinet_key IN (
                          '__current__',
                          '__disabled_current_fallback__'
                      )
                      OR selected_cabinet_name LIKE '%Выйти%'
                      OR selected_cabinet_name LIKE '%Акции Kaspi%'
                      OR selected_cabinet_name LIKE '%Акции с бонусами%'"""
            )
            # Old builds stored thousands of zero-spend product rows from
            # Marketing exports. They do not affect profit and only clutter
            # detailed reports / matching.
            self.conn.execute(
                """DELETE FROM marketing_report_rows
                   WHERE ABS(COALESCE(amount,0)) < 0.000001"""
            )

            # Normalize fake order code 0 from old Marketing imports.
            self.conn.execute(
                """UPDATE marketing_report_rows
                   SET order_code=NULL
                   WHERE TRIM(COALESCE(order_code,'')) IN
                         ('0','0.0','0,0','-','—','None','null')"""
            )

            # Rebuild advertising campaign source cache from current active
            # campaigns. Historical report rows are kept because they may be
            # needed for past-day profit calculations.
            self.conn.execute(
                """DELETE FROM marketing_campaign_sources
                   WHERE section_key IN ('external','goods')"""
            )

            # User intentionally excludes these Marketing sections.
            # Remove any old cached rows so they cannot affect profit.
            self.conn.execute(
                """DELETE FROM marketing_report_rows
                   WHERE section_key IN ('brands','shop_promotions','qr')"""
            )
            self.conn.execute(
                """DELETE FROM marketing_campaign_sources
                   WHERE section_key IN ('brands','shop_promotions','qr')"""
            )
            self.conn.commit()

    def _backfill_delivery_metadata(self):
        rows = self.conn.execute(
            "SELECT shop_id,id,raw_json FROM orders"
        ).fetchall()

        updated = 0

        for row in rows:
            if not row["raw_json"]:
                continue

            try:
                payload = json.loads(row["raw_json"])
                attrs = payload.get("attributes") or {}
            except Exception:
                continue

            seller_known = (
                "deliveryCostForSeller" in attrs
                and attrs.get("deliveryCostForSeller") is not None
            )

            self.conn.execute(
                """UPDATE orders
                   SET delivery_cost_for_seller=?,
                       delivery_cost_for_seller_known=?,
                       delivery_cost=?,
                       is_kaspi_delivery=?,
                       delivery_mode=?
                   WHERE shop_id=? AND id=?""",
                (
                    float(attrs.get("deliveryCostForSeller") or 0),
                    1 if seller_known else 0,
                    float(attrs.get("deliveryCost") or 0),
                    (
                        1 if attrs.get("isKaspiDelivery") is True
                        else 0 if attrs.get("isKaspiDelivery") is False
                        else None
                    ),
                    attrs.get("deliveryMode"),
                    row["shop_id"],
                    row["id"],
                ),
            )
            updated += 1

        if updated:
            self.conn.commit()
            print(
                f"Delivery metadata backfilled for {updated} orders."
            )

    # ---------- Shops ----------

    def shops_for_user(self, user_id):
        return self.conn.execute(
            """SELECT id,name,is_active,created_at,updated_at
               FROM shops
               WHERE telegram_user_id=?
               ORDER BY is_active DESC, name COLLATE NOCASE""",
            (str(user_id),),
        ).fetchall()

    def all_shops(self):
        return self.conn.execute(
            "SELECT * FROM shops ORDER BY id"
        ).fetchall()

    def shop_count_for_user(self, user_id):
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM shops WHERE telegram_user_id=?",
            (str(user_id),),
        ).fetchone()
        return int(row["c"] if row else 0)

    def shop(self, shop_id, user_id=None):
        if user_id is None:
            return self.conn.execute(
                "SELECT * FROM shops WHERE id=?",
                (shop_id,),
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM shops WHERE id=? AND telegram_user_id=?",
            (shop_id, str(user_id)),
        ).fetchone()

    def active_shop(self, user_id):
        row = self.conn.execute(
            """SELECT * FROM shops
               WHERE telegram_user_id=? AND is_active=1
               LIMIT 1""",
            (str(user_id),),
        ).fetchone()
        if row:
            return row

        row = self.conn.execute(
            """SELECT * FROM shops
               WHERE telegram_user_id=?
               ORDER BY id LIMIT 1""",
            (str(user_id),),
        ).fetchone()
        if row:
            self.set_active_shop(user_id, row["id"])
            return self.shop(row["id"], user_id)
        return None

    def add_shop(self, user_id, name, token):
        now = datetime.now(timezone.utc).isoformat()
        has_any = self.conn.execute(
            "SELECT 1 FROM shops WHERE telegram_user_id=? LIMIT 1",
            (str(user_id),),
        ).fetchone()
        is_active = 0 if has_any else 1

        cur = self.conn.execute(
            """INSERT INTO shops
               (telegram_user_id,name,token_encrypted,is_active,created_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (
                str(user_id),
                name.strip(),
                encrypt_token(token),
                is_active,
                now,
                now,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def set_active_shop(self, user_id, shop_id):
        self.conn.execute(
            "UPDATE shops SET is_active=0 WHERE telegram_user_id=?",
            (str(user_id),),
        )
        self.conn.execute(
            """UPDATE shops
               SET is_active=1, updated_at=?
               WHERE id=? AND telegram_user_id=?""",
            (
                datetime.now(timezone.utc).isoformat(),
                shop_id,
                str(user_id),
            ),
        )
        self.conn.commit()

    def update_shop_token(self, user_id, shop_id, token):
        self.conn.execute(
            """UPDATE shops
               SET token_encrypted=?, updated_at=?
               WHERE id=? AND telegram_user_id=?""",
            (
                encrypt_token(token),
                datetime.now(timezone.utc).isoformat(),
                shop_id,
                str(user_id),
            ),
        )
        self.conn.commit()

    def rename_shop(self, user_id, shop_id, new_name):
        self.conn.execute(
            """UPDATE shops SET name=?, updated_at=?
               WHERE id=? AND telegram_user_id=?""",
            (
                new_name.strip(),
                datetime.now(timezone.utc).isoformat(),
                shop_id,
                str(user_id),
            ),
        )
        self.conn.commit()

    def delete_shop(self, user_id, shop_id):
        row = self.shop(shop_id, user_id)
        if not row:
            return
        was_active = bool(row["is_active"])
        self.conn.execute(
            "DELETE FROM shops WHERE id=? AND telegram_user_id=?",
            (shop_id, str(user_id)),
        )
        self.conn.commit()
        if was_active:
            remaining = self.conn.execute(
                """SELECT id FROM shops
                   WHERE telegram_user_id=?
                   ORDER BY id LIMIT 1""",
                (str(user_id),),
            ).fetchone()
            if remaining:
                self.set_active_shop(user_id, remaining["id"])

    # ---------- Kaspi Marketing ----------

    def marketing_account(self, shop_id):
        return self.conn.execute(
            "SELECT * FROM marketing_accounts WHERE shop_id=?",
            (shop_id,),
        ).fetchone()

    def save_marketing_account(self, shop_id, login, password, status="connected", last_error=None):
        self.conn.execute(
            """INSERT INTO marketing_accounts
               (shop_id,login_encrypted,password_encrypted,status,last_error,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(shop_id) DO UPDATE SET
                 login_encrypted=excluded.login_encrypted,
                 password_encrypted=excluded.password_encrypted,
                 status=excluded.status,
                 last_error=excluded.last_error,
                 updated_at=excluded.updated_at""",
            (
                shop_id,
                encrypt_token(str(login)),
                encrypt_token(str(password)),
                str(status),
                last_error,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def marketing_credentials(self, shop_id):
        row = self.marketing_account(shop_id)
        if not row:
            return None
        try:
            return (
                decrypt_token(row["login_encrypted"]),
                decrypt_token(row["password_encrypted"]),
            )
        except Exception:
            return None

    def update_marketing_status(self, shop_id, status, last_error=None):
        self.conn.execute(
            """UPDATE marketing_accounts
               SET status=?,last_error=?,updated_at=?
               WHERE shop_id=?""",
            (
                str(status),
                last_error,
                datetime.now(timezone.utc).isoformat(),
                shop_id,
            ),
        )
        self.conn.commit()

    def set_marketing_cabinet(
        self,
        shop_id,
        cabinet_key,
        cabinet_name,
    ):
        self.conn.execute(
            """UPDATE marketing_accounts
               SET selected_cabinet_key=?,
                   selected_cabinet_name=?,
                   selected_merchant_id=NULL,
                   updated_at=?
               WHERE shop_id=?""",
            (
                str(cabinet_key or cabinet_name),
                str(cabinet_name),
                datetime.now(timezone.utc).isoformat(),
                shop_id,
            ),
        )
        self.conn.commit()

    def set_marketing_merchant_id(
        self,
        shop_id,
        merchant_id,
    ):
        self.conn.execute(
            """UPDATE marketing_accounts
               SET selected_merchant_id=?,
                   updated_at=?
               WHERE shop_id=?""",
            (
                str(merchant_id) if merchant_id else None,
                datetime.now(timezone.utc).isoformat(),
                shop_id,
            ),
        )
        self.conn.commit()

    def marketing_campaign_source(
        self,
        shop_id,
        section_key,
        campaign_name,
    ):
        return self.conn.execute(
            """SELECT *
               FROM marketing_campaign_sources
               WHERE shop_id=?
                 AND section_key=?
                 AND campaign_name=?
               LIMIT 1""",
            (
                shop_id,
                section_key,
                str(campaign_name),
            ),
        ).fetchone()

    def upsert_marketing_campaign_source(
        self,
        shop_id,
        cabinet_key,
        cabinet_name,
        merchant_id,
        section_key,
        campaign_id,
        campaign_name,
        report_url,
        file_kind,
    ):
        self.conn.execute(
            """INSERT INTO marketing_campaign_sources
               (shop_id,cabinet_key,cabinet_name,merchant_id,
                section_key,campaign_id,campaign_name,report_url,
                file_kind,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(shop_id,section_key,campaign_name) DO UPDATE SET
                 cabinet_key=excluded.cabinet_key,
                 cabinet_name=excluded.cabinet_name,
                 merchant_id=COALESCE(excluded.merchant_id,merchant_id),
                 campaign_id=COALESCE(excluded.campaign_id,campaign_id),
                 report_url=COALESCE(excluded.report_url,report_url),
                 file_kind=COALESCE(excluded.file_kind,file_kind),
                 updated_at=excluded.updated_at""",
            (
                shop_id,
                cabinet_key,
                cabinet_name,
                str(merchant_id) if merchant_id else None,
                section_key,
                str(campaign_id) if campaign_id else None,
                str(campaign_name),
                report_url,
                file_kind,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def clear_marketing_cabinet(self, shop_id):
        self.conn.execute(
            """UPDATE marketing_accounts
               SET selected_cabinet_key=NULL,
                   selected_cabinet_name=NULL,
                   selected_merchant_id=NULL,
                   updated_at=?
               WHERE shop_id=?""",
            (
                datetime.now(timezone.utc).isoformat(),
                shop_id,
            ),
        )
        self.conn.commit()

    def resolve_marketing_product(
        self,
        shop_id,
        product_code=None,
        sku=None,
        product_name=None,
    ):
        if product_code:
            row = self.conn.execute(
                """SELECT sku,name,kaspi_product_code
                   FROM order_items
                   WHERE shop_id=? AND kaspi_product_code=?
                   ORDER BY rowid DESC LIMIT 1""",
                (shop_id, str(product_code).strip()),
            ).fetchone()
            if row:
                return {
                    "sku": row["sku"],
                    "name": row["name"],
                    "product_code": row["kaspi_product_code"],
                    "quality": "kaspi_product_code",
                }

        if sku:
            row = self.conn.execute(
                """SELECT sku,name,kaspi_product_code
                   FROM order_items
                   WHERE shop_id=? AND sku=?
                   ORDER BY rowid DESC LIMIT 1""",
                (shop_id, str(sku).strip()),
            ).fetchone()
            if row:
                return {
                    "sku": row["sku"],
                    "name": row["name"],
                    "product_code": row["kaspi_product_code"],
                    "quality": "sku",
                }

        if product_name:
            row = self.conn.execute(
                """SELECT sku,name,kaspi_product_code
                   FROM order_items
                   WHERE shop_id=?
                     AND LOWER(TRIM(name))=LOWER(TRIM(?))
                   ORDER BY rowid DESC LIMIT 1""",
                (shop_id, str(product_name).strip()),
            ).fetchone()
            if row:
                return {
                    "sku": row["sku"],
                    "name": row["name"],
                    "product_code": row["kaspi_product_code"],
                    "quality": "name",
                }

        return {
            "sku": sku,
            "name": product_name,
            "product_code": product_code,
            "quality": "unmatched",
        }

    def upsert_marketing_report_row(
        self,
        shop_id,
        cabinet_key,
        cabinet_name,
        section_key,
        section_name,
        campaign_name,
        report_date,
        product_code,
        sku,
        product_name,
        order_code,
        amount,
        clicks,
        impressions,
        orders_count,
        revenue,
        mapping_quality,
        source_file,
        fingerprint,
        sync_run_id,
        raw_json,
    ):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO marketing_report_rows
               (shop_id,cabinet_key,cabinet_name,section_key,section_name,
                campaign_name,report_date,product_code,sku,product_name,
                order_code,amount,clicks,impressions,orders_count,revenue,
                mapping_quality,source_file,fingerprint,sync_run_id,raw_json,synced_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(shop_id,fingerprint) DO UPDATE SET
                 cabinet_key=excluded.cabinet_key,
                 cabinet_name=excluded.cabinet_name,
                 section_key=excluded.section_key,
                 section_name=excluded.section_name,
                 campaign_name=excluded.campaign_name,
                 report_date=excluded.report_date,
                 product_code=excluded.product_code,
                 sku=excluded.sku,
                 product_name=excluded.product_name,
                 order_code=excluded.order_code,
                 amount=excluded.amount,
                 clicks=excluded.clicks,
                 impressions=excluded.impressions,
                 orders_count=excluded.orders_count,
                 revenue=excluded.revenue,
                 mapping_quality=excluded.mapping_quality,
                 source_file=excluded.source_file,
                 sync_run_id=excluded.sync_run_id,
                 raw_json=excluded.raw_json,
                 synced_at=excluded.synced_at""",
            (
                shop_id, cabinet_key, cabinet_name, section_key, section_name,
                campaign_name, report_date, product_code, sku, product_name,
                order_code, float(amount or 0), clicks, impressions,
                orders_count, revenue, mapping_quality, source_file,
                fingerprint, sync_run_id, raw_json, now,
            ),
        )
        self.conn.commit()

    def delete_marketing_report_slice(
        self,
        shop_id,
        cabinet_key,
        section_key,
        campaign_name,
        report_date,
    ):
        self.conn.execute(
            """DELETE FROM marketing_report_rows
               WHERE shop_id=?
                 AND COALESCE(cabinet_key,'')=COALESCE(?, '')
                 AND section_key=?
                 AND COALESCE(campaign_name,'')=COALESCE(?, '')
                 AND COALESCE(report_date,'')=COALESCE(?, '')""",
            (
                shop_id,
                cabinet_key,
                section_key,
                campaign_name,
                report_date,
            ),
        )
        self.conn.commit()

    def marketing_rows_for_period(
        self,
        shop_id,
        start_date,
        end_date,
    ):
        return self.conn.execute(
            """SELECT * FROM marketing_report_rows
               WHERE shop_id=?
                 AND report_date>=?
                 AND report_date<=?
               ORDER BY report_date,section_name,campaign_name,product_name,id""",
            (
                shop_id,
                str(start_date),
                str(end_date),
            ),
        ).fetchall()

    def marketing_rows_for_date(self, shop_id, report_date):
        return self.conn.execute(
            """SELECT * FROM marketing_report_rows
               WHERE shop_id=? AND report_date=?
               ORDER BY section_name,campaign_name,product_name,id""",
            (shop_id, str(report_date)),
        ).fetchall()

    def marketing_spend_for_date(self, shop_id, report_date):
        row = self.conn.execute(
            """SELECT COALESCE(SUM(amount),0) AS total
               FROM marketing_report_rows
               WHERE shop_id=? AND report_date=?""",
            (shop_id, str(report_date)),
        ).fetchone()
        return float(row["total"] or 0)

    def marketing_spend_by_section_for_date(self, shop_id, report_date):
        return self.conn.execute(
            """SELECT section_key,section_name,
                      COALESCE(SUM(amount),0) AS amount,
                      COUNT(*) AS rows_count,
                      SUM(CASE WHEN order_code IS NULL OR TRIM(order_code)=''
                               THEN 1 ELSE 0 END) AS no_order_rows
               FROM marketing_report_rows
               WHERE shop_id=? AND report_date=?
               GROUP BY section_key,section_name
               ORDER BY amount DESC,section_name""",
            (shop_id, str(report_date)),
        ).fetchall()

    def marketing_spend_recent_days(self, shop_id, limit=14):
        return self.conn.execute(
            """SELECT report_date,
                      COALESCE(SUM(amount),0) AS amount,
                      COUNT(*) AS rows_count
               FROM marketing_report_rows
               WHERE shop_id=? AND report_date IS NOT NULL
               GROUP BY report_date
               ORDER BY report_date DESC
               LIMIT ?""",
            (shop_id, int(limit)),
        ).fetchall()

    def cleanup_marketing_rows_after_sync(
        self,
        shop_id,
        cabinet_key,
        sync_run_id,
    ):
        self.conn.execute(
            """DELETE FROM marketing_report_rows
               WHERE shop_id=?
                 AND COALESCE(cabinet_key,'')=COALESCE(?, '')
                 AND COALESCE(sync_run_id, -1)<>?""",
            (
                shop_id,
                cabinet_key,
                int(sync_run_id),
            ),
        )
        self.conn.commit()

    def start_marketing_sync_run(self, shop_id, cabinet_key, cabinet_name):
        cur = self.conn.execute(
            """INSERT INTO marketing_sync_runs
               (shop_id,cabinet_key,cabinet_name,started_at)
               VALUES(?,?,?,?)""",
            (
                shop_id,
                cabinet_key,
                cabinet_name,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_marketing_sync_run(
        self,
        run_id,
        files_downloaded,
        rows_imported,
        sections_seen,
        error=None,
    ):
        self.conn.execute(
            """UPDATE marketing_sync_runs
               SET finished_at=?,files_downloaded=?,rows_imported=?,
                   sections_seen=?,error=?
               WHERE id=?""",
            (
                datetime.now(timezone.utc).isoformat(),
                int(files_downloaded),
                int(rows_imported),
                json.dumps(sections_seen, ensure_ascii=False),
                error,
                run_id,
            ),
        )
        self.conn.commit()

    def last_marketing_sync(self, shop_id):
        return self.conn.execute(
            """SELECT * FROM marketing_sync_runs
               WHERE shop_id=? ORDER BY id DESC LIMIT 1""",
            (shop_id,),
        ).fetchone()

    def delete_marketing_account(self, shop_id):
        self.conn.execute(
            "DELETE FROM marketing_accounts WHERE shop_id=?",
            (shop_id,),
        )
        self.conn.commit()

    # ---------- Orders ----------

    def order_by_code(self, shop_id, code):
        return self.conn.execute(
            """SELECT * FROM orders
               WHERE shop_id=? AND code=?
               ORDER BY last_seen_at DESC
               LIMIT 1""",
            (shop_id, str(code)),
        ).fetchone()

    def get_order(self, shop_id, order_id):
        return self.conn.execute(
            "SELECT * FROM orders WHERE shop_id=? AND id=?",
            (shop_id, order_id),
        ).fetchone()

    def upsert_order(self, shop_id, order):
        attrs = order.get("attributes") or {}
        now = datetime.now(timezone.utc).isoformat()
        old = self.get_order(shop_id, order["id"])
        status = attrs.get("status")

        if old is None:
            self.conn.execute(
                """INSERT INTO orders
                   (shop_id,id,code,total_price,status,state,creation_ms,completion_ms,
                    delivery_cost_for_seller,delivery_cost_for_seller_known,
                    delivery_cost,is_kaspi_delivery,delivery_mode,
                    returned_to_warehouse,raw_json,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    shop_id,
                    order["id"],
                    attrs.get("code"),
                    float(attrs.get("totalPrice") or 0),
                    status,
                    attrs.get("state"),
                    attrs.get("creationDate"),
                    attrs.get("completionDate"),
                    float(attrs.get("deliveryCostForSeller") or 0),
                    (
                        1
                        if (
                            "deliveryCostForSeller" in attrs
                            and attrs.get("deliveryCostForSeller") is not None
                        )
                        else 0
                    ),
                    float(attrs.get("deliveryCost") or 0),
                    (
                        1 if attrs.get("isKaspiDelivery") is True
                        else 0 if attrs.get("isKaspiDelivery") is False
                        else None
                    ),
                    attrs.get("deliveryMode"),
                    1 if attrs.get("returnedToWarehouse") else 0,
                    json.dumps(order, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            if status:
                self.conn.execute(
                    """INSERT INTO order_status_history
                       (shop_id,order_id,old_status,new_status,observed_at,is_initial)
                       VALUES(?,?,?,?,?,1)""",
                    (shop_id, order["id"], None, status, now),
                )
        else:
            old_status = old["status"]
            self.conn.execute(
                """UPDATE orders SET
                   code=?,total_price=?,status=?,state=?,creation_ms=?,completion_ms=?,
                   delivery_cost_for_seller=?,
                   delivery_cost_for_seller_known=?,
                   delivery_cost=?,
                   is_kaspi_delivery=?,
                   delivery_mode=?,
                   returned_to_warehouse=?,raw_json=?,last_seen_at=?
                   WHERE shop_id=? AND id=?""",
                (
                    attrs.get("code"),
                    float(attrs.get("totalPrice") or 0),
                    status,
                    attrs.get("state"),
                    attrs.get("creationDate"),
                    attrs.get("completionDate"),
                    float(attrs.get("deliveryCostForSeller") or 0),
                    (
                        1
                        if (
                            "deliveryCostForSeller" in attrs
                            and attrs.get("deliveryCostForSeller") is not None
                        )
                        else 0
                    ),
                    float(attrs.get("deliveryCost") or 0),
                    (
                        1 if attrs.get("isKaspiDelivery") is True
                        else 0 if attrs.get("isKaspiDelivery") is False
                        else None
                    ),
                    attrs.get("deliveryMode"),
                    1 if attrs.get("returnedToWarehouse") else 0,
                    json.dumps(order, ensure_ascii=False),
                    now,
                    shop_id,
                    order["id"],
                ),
            )
            if status and old_status != status:
                self.conn.execute(
                    """INSERT INTO order_status_history
                       (shop_id,order_id,old_status,new_status,observed_at,is_initial)
                       VALUES(?,?,?,?,?,0)""",
                    (shop_id, order["id"], old_status, status, now),
                )
        self.conn.commit()


    def returns_discovered_between(self, shop_id, start_date, end_date):
        """First observed RETURNED status; Kaspi API has no verified refund timestamp here."""
        start, end = business_day_bounds_dt(start_date, end_date)
        start_utc = start.astimezone(timezone.utc).isoformat()
        end_utc = end.astimezone(timezone.utc).isoformat()
        return self.conn.execute(
            """SELECT o.*, h.first_return_observed_at
               FROM orders o JOIN (
                   SELECT shop_id, order_id, MIN(observed_at) AS first_return_observed_at
                   FROM order_status_history WHERE new_status='RETURNED'
                   GROUP BY shop_id, order_id
               ) h ON h.shop_id=o.shop_id AND h.order_id=o.id
               WHERE o.shop_id=? AND o.status='RETURNED'
                 AND h.first_return_observed_at>=?
                 AND h.first_return_observed_at<?
               ORDER BY h.first_return_observed_at DESC""",
            (shop_id, start_utc, end_utc),
        ).fetchall()


    def upsert_item(self, shop_id, order_id, entry, product, merchant):
        attrs = entry.get("attributes") or {}
        product_data = (product or {}).get("data") or {}
        product_attrs = product_data.get("attributes") or {}
        merchant_data = (merchant or {}).get("data") or {}
        merchant_attrs = merchant_data.get("attributes") or {}

        category = attrs.get("category")
        if isinstance(category, dict):
            category = category.get("title") or category.get("code")

        sku = merchant_attrs.get("code") or product_attrs.get("code")
        name = merchant_attrs.get("name") or product_attrs.get("name")
        image_url = (
            extract_image_url(merchant)
            or extract_image_url(product)
            or extract_image_url(entry)
        )

        self.conn.execute(
            """INSERT INTO order_items
               (shop_id,entry_id,order_id,quantity,total_price,base_price,delivery_cost,
                master_product_id,kaspi_product_code,sku,name,category,image_url,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(shop_id,entry_id) DO UPDATE SET
                 order_id=excluded.order_id,
                 quantity=excluded.quantity,
                 total_price=excluded.total_price,
                 base_price=excluded.base_price,
                 delivery_cost=excluded.delivery_cost,
                 master_product_id=excluded.master_product_id,
                 kaspi_product_code=excluded.kaspi_product_code,
                 sku=excluded.sku,
                 name=excluded.name,
                 category=excluded.category,
                 image_url=COALESCE(excluded.image_url, order_items.image_url),
                 raw_json=excluded.raw_json""",
            (
                shop_id,
                entry["id"],
                order_id,
                float(attrs.get("quantity") or 0),
                float(attrs.get("totalPrice") or 0),
                float(attrs.get("basePrice") or 0),
                float(attrs.get("deliveryCost") or 0),
                product_data.get("id"),
                product_attrs.get("code"),
                sku,
                name,
                category,
                image_url,
                json.dumps(entry, ensure_ascii=False),
            ),
        )
        self.conn.commit()

        # v4.8: no model/color grouping.
        # Cost is stored only for the exact SKU.

    # ---------- Financial settings ----------

    def financial_settings(self, shop_id):
        row = self.conn.execute(
            """SELECT * FROM shop_financial_settings
               WHERE shop_id=?""",
            (shop_id,),
        ).fetchone()

        if row:
            return row

        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO shop_financial_settings
               (shop_id,default_commission_rate,default_tax_rate,
                default_acquiring_rate,updated_at)
               VALUES(?,?,?,?,?)""",
            (
                shop_id,
                15.5,
                3.0,
                0.0,
                now,
            ),
        )
        self.conn.commit()

        return self.conn.execute(
            """SELECT * FROM shop_financial_settings
               WHERE shop_id=?""",
            (shop_id,),
        ).fetchone()

    def update_financial_setting(
        self,
        shop_id,
        field,
        value,
    ):
        allowed = {
            "default_commission_rate",
            "default_tax_rate",
            "default_acquiring_rate",
        }
        if field not in allowed:
            raise ValueError("Unsupported financial setting")

        self.financial_settings(shop_id)

        self.conn.execute(
            f"""UPDATE shop_financial_settings
                SET {field}=?, updated_at=?
                WHERE shop_id=?""",
            (
                float(value),
                datetime.now(timezone.utc).isoformat(),
                shop_id,
            ),
        )
        self.conn.commit()

    # ---------- Costs ----------

    def upsert_costs(self, shop_id, rows):
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for row in rows:
            sku = str(row.get("sku") or "").strip()
            if not sku:
                continue
            self.conn.execute(
                """INSERT INTO costs
                   (shop_id,sku,cost_price,commission_rate,tax_rate,
                    acquiring_rate,packaging_cost,other_cost,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(shop_id,sku) DO UPDATE SET
                     cost_price=excluded.cost_price,
                     commission_rate=excluded.commission_rate,
                     tax_rate=excluded.tax_rate,
                     acquiring_rate=excluded.acquiring_rate,
                     packaging_cost=excluded.packaging_cost,
                     other_cost=excluded.other_cost,
                     updated_at=excluded.updated_at""",
                (
                    shop_id,
                    sku,
                    float(row.get("cost_price") or 0),
                    float(row.get("commission_rate") or 0),
                    float(row.get("tax_rate") or 0),
                    float(row.get("acquiring_rate") or 0),
                    float(row.get("packaging_cost") or 0),
                    float(row.get("other_cost") or 0),
                    now,
                ),
            )
            count += 1
        self.conn.commit()
        return count

    def seed_model_costs_from_exact_costs(self, shop_id):
        """
        Conservative migration: if known exact SKU costs for one normalized
        model all have the same cost_price, save that as the model template.
        """
        rows = self.conn.execute(
            """SELECT DISTINCT oi.sku,oi.name,oi.category,c.cost_price
               FROM order_items oi
               JOIN costs c
                 ON c.shop_id=oi.shop_id AND c.sku=oi.sku
               WHERE oi.shop_id=?
                 AND oi.sku IS NOT NULL""",
            (shop_id,),
        ).fetchall()

        grouped = {}
        for row in rows:
            key = model_cost_group_key(row["name"], row["category"])
            if self.sku_is_ambiguous(shop_id, row["sku"]):
                continue
            if not key:
                continue
            grouped.setdefault(key, []).append(row)

        for key, group in grouped.items():
            prices = {
                round(float(row["cost_price"]), 6)
                for row in group
            }
            if len(prices) == 1:
                price = float(group[0]["cost_price"])
                self.upsert_model_cost(
                    shop_id,
                    key,
                    model_display_name(group[0]["name"]),
                    price,
                )

    def cost(self, shop_id, sku):
        if not sku:
            return None
        return self.conn.execute(
            "SELECT * FROM costs WHERE shop_id=? AND sku=?",
            (shop_id, str(sku)),
        ).fetchone()

    def sku_is_ambiguous(self, shop_id, sku):
        if not sku:
            return True
        rows = self.conn.execute(
            "SELECT DISTINCT name, category, kaspi_product_code, master_product_id, sku "
            "FROM order_items WHERE shop_id=? AND sku=?",
            (shop_id, str(sku)),
        ).fetchall()
        groups = set()
        for row in rows:
            key = model_cost_group_key(row["name"], row["category"])
            groups.add(key or product_cost_identity(row))
            if len(groups) > 1:
                return True
        return False

    def cost_for_item(self, shop_id, item):
        """Always use item identity. Never reuse SKU cost for another model/category."""
        if item is None:
            return None
        identity = product_cost_identity(item)
        specific = self.conn.execute(
            "SELECT cost_price FROM product_cost_overrides WHERE shop_id=? AND identity_key=?",
            (shop_id, identity),
        ).fetchone()
        if specific:
            return self._resolved_cost_record(shop_id, item, specific["cost_price"])
        group_key = model_cost_group_key(item["name"], item["category"])
        if group_key:
            model = self.model_cost(shop_id, group_key)
            if model:
                return self._resolved_cost_record(shop_id, item, model["cost_price"])
        sku = item["sku"]
        if self.sku_is_ambiguous(shop_id, sku):
            return None
        return self.cost(shop_id, sku)

    def _resolved_cost_record(self, shop_id, item, price):
        # Legacy SKU-level commissions are safe only if there is no collision.
        legacy = None
        if not self.sku_is_ambiguous(shop_id, item["sku"]):
            legacy = self.cost(shop_id, item["sku"])
        fields = ("commission_rate", "tax_rate", "acquiring_rate", "packaging_cost", "other_cost")
        return dict(cost_price=float(price), **{
            f: float(legacy[f] or 0) if legacy else 0.0 for f in fields
        })

    def set_cost_for_item(self, shop_id, item, price, *, whole_model=False):
        """Explicit model assignment or exact product override; never mutates shared SKU cost."""
        price = float(price)
        if price < 0:
            raise ValueError("Negative cost")
        group_key = model_cost_group_key(item["name"], item["category"])
        if whole_model and group_key:
            self.upsert_model_cost(shop_id, group_key, model_display_name(item["name"]), price)
            return "model"
        self.conn.execute(
            """INSERT INTO product_cost_overrides
               (shop_id,identity_key,sku,category,name,cost_price,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(shop_id,identity_key) DO UPDATE SET
                 cost_price=excluded.cost_price,updated_at=excluded.updated_at""",
            (shop_id, product_cost_identity(item), item["sku"],
             cost_product_type(item["name"], item["category"]), item["name"],
             price, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        return "product"

    def same_model_products(self, shop_id, item, limit=50):
        key = model_cost_group_key(item["name"], item["category"])
        if not key:
            return [item]
        rows = self.conn.execute(
            """SELECT DISTINCT name,category,sku,kaspi_product_code,master_product_id
               FROM order_items WHERE shop_id=?""", (shop_id,),
        ).fetchall()
        return [row for row in rows if model_cost_group_key(row["name"], row["category"]) == key][:limit]

    def model_cost(self, shop_id, group_key):
        if not group_key:
            return None
        return self.conn.execute(
            """SELECT * FROM model_costs
               WHERE shop_id=? AND group_key=?""",
            (shop_id, str(group_key)),
        ).fetchone()

    def upsert_model_cost(self, shop_id, group_key, display_name, cost_price):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO model_costs
               (shop_id,group_key,display_name,cost_price,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(shop_id,group_key) DO UPDATE SET
                 display_name=excluded.display_name,
                 cost_price=excluded.cost_price,
                 updated_at=excluded.updated_at""",
            (
                shop_id,
                str(group_key),
                str(display_name or ""),
                float(cost_price),
                now,
            ),
        )
        self.conn.commit()

    def matching_legacy_model_variants(self, shop_id, group_key):
        rows = self.conn.execute(
            """SELECT DISTINCT sku,name,category
               FROM order_items
               WHERE shop_id=?
                 AND sku IS NOT NULL
                 AND TRIM(sku)<>''""",
            (shop_id,),
        ).fetchall()

        result = []
        for row in rows:
            if legacy_model_cost_group_key(
                row["name"],
                row["category"],
            ) == group_key:
                result.append(row)

        return result

    def unsafe_model_groups(self, shop_id):
        rows = self.conn.execute(
            """SELECT * FROM model_costs
               WHERE shop_id=?
               ORDER BY updated_at DESC""",
            (shop_id,),
        ).fetchall()

        suspicious = []

        for group in rows:
            group_key = group["group_key"]
            suffix = group_key.split("::", 1)[-1]
            variants = self.matching_legacy_model_variants(
                shop_id,
                group_key,
            )

            unsafe_name = not is_safe_normalized_model_name(suffix)
            too_many = len(variants) > MAX_AUTO_MODEL_VARIANTS

            if unsafe_name or too_many:
                suspicious.append({
                    "group": group,
                    "variants": variants,
                    "unsafe_name": unsafe_name,
                    "too_many": too_many,
                })

        return suspicious

    def repair_unsafe_model_group(self, shop_id, group_key):
        group = self.conn.execute(
            """SELECT * FROM model_costs
               WHERE shop_id=? AND group_key=?""",
            (shop_id, group_key),
        ).fetchone()

        if not group:
            return 0

        variants = self.matching_legacy_model_variants(
            shop_id,
            group_key,
        )
        target_skus = [row["sku"] for row in variants if row["sku"]]

        if not target_skus:
            self.conn.execute(
                "DELETE FROM model_costs WHERE shop_id=? AND group_key=?",
                (shop_id, group_key),
            )
            self.conn.commit()
            return 0

        placeholders = ",".join("?" for _ in target_skus)
        params = [shop_id, *target_skus]
        rows = self.conn.execute(
            f"""SELECT * FROM costs
                WHERE shop_id=? AND sku IN ({placeholders})""",
            params,
        ).fetchall()

        now = datetime.now(timezone.utc).isoformat()
        group_price = float(group["cost_price"])
        backed_up = 0

        # Only remove rows that currently equal the v4.6 model price.
        # Everything is backed up first and can be restored.
        for row in rows:
            if abs(float(row["cost_price"]) - group_price) > 0.000001:
                continue

            self.conn.execute(
                """INSERT INTO cost_repair_backup
                   (shop_id,sku,cost_price,commission_rate,tax_rate,
                    packaging_cost,other_cost,original_updated_at,
                    backup_reason,backed_up_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    shop_id,
                    row["sku"],
                    row["cost_price"],
                    row["commission_rate"],
                    row["tax_rate"],
                    row["packaging_cost"],
                    row["other_cost"],
                    row["updated_at"],
                    f"unsafe_model_group:{group_key}",
                    now,
                ),
            )

            self.conn.execute(
                "DELETE FROM costs WHERE shop_id=? AND sku=?",
                (shop_id, row["sku"]),
            )
            backed_up += 1

        self.conn.execute(
            "DELETE FROM model_costs WHERE shop_id=? AND group_key=?",
            (shop_id, group_key),
        )
        self.conn.commit()
        return backed_up

    def restore_last_repair(self, shop_id):
        rows = self.conn.execute(
            """SELECT * FROM cost_repair_backup
               WHERE shop_id=? AND restored_at IS NULL
               ORDER BY id DESC""",
            (shop_id,),
        ).fetchall()

        if not rows:
            return 0

        restored = 0
        now = datetime.now(timezone.utc).isoformat()

        for row in rows:
            self.conn.execute(
                """INSERT INTO costs
                   (shop_id,sku,cost_price,commission_rate,tax_rate,
                    packaging_cost,other_cost,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(shop_id,sku) DO UPDATE SET
                     cost_price=excluded.cost_price,
                     commission_rate=excluded.commission_rate,
                     tax_rate=excluded.tax_rate,
                     packaging_cost=excluded.packaging_cost,
                     other_cost=excluded.other_cost,
                     updated_at=excluded.updated_at""",
                (
                    shop_id,
                    row["sku"],
                    row["cost_price"],
                    row["commission_rate"],
                    row["tax_rate"],
                    row["packaging_cost"],
                    row["other_cost"],
                    row["original_updated_at"] or now,
                ),
            )
            self.conn.execute(
                "UPDATE cost_repair_backup SET restored_at=? WHERE id=?",
                (now, row["id"]),
            )
            restored += 1

        self.conn.commit()
        return restored

    def matching_model_variants(self, shop_id, group_key):
        rows = self.conn.execute(
            """SELECT DISTINCT sku,name,category
               FROM order_items
               WHERE shop_id=?
                 AND sku IS NOT NULL
                 AND TRIM(sku)<>''""",
            (shop_id,),
        ).fetchall()

        result = []
        for row in rows:
            if model_cost_group_key(row["name"], row["category"]) == group_key:
                result.append(row)
        return result

    def apply_model_cost(self, shop_id, group_key, display_name, cost_price):
        """
        Auto-apply only to a small, safely identified model family.
        Never fan one value out to dozens of SKUs.
        """
        if not group_key:
            return []

        variants = self.matching_model_variants(shop_id, group_key)

        if (
            not variants
            or len(variants) > MAX_AUTO_MODEL_VARIANTS
        ):
            return []

        self.upsert_model_cost(
            shop_id,
            group_key,
            display_name,
            cost_price,
        )

        # Model price is resolved by name + product type at calculation time.
        # Deliberately do not write SKU costs: a SKU may label both a hat and a stole.

        return variants

    def inherit_model_cost(self, shop_id, sku, name, category=None):
        """
        When a new colour first appears in an order, automatically inherit
        the saved cost of its model instead of asking the user again.
        """
        if not sku or self.cost(shop_id, sku):
            return False

        group_key = model_cost_group_key(name, category)
        saved = self.model_cost(shop_id, group_key)

        if not saved:
            return False

        variants = self.matching_model_variants(
            shop_id,
            group_key,
        )

        if len(variants) > MAX_AUTO_MODEL_VARIANTS:
            return False

        # Price is inherited dynamically by cost_for_item, without corrupting
        # a shared SKU. No legacy cost write is necessary.
        return True

    def propagate_model_costs(self, shop_id):
        """Backfill costs for variants already in DB but not yet assigned."""
        rows = self.conn.execute(
            """SELECT DISTINCT oi.sku,oi.name,oi.category
               FROM order_items oi
               LEFT JOIN costs c
                 ON c.shop_id=oi.shop_id AND c.sku=oi.sku
               WHERE oi.shop_id=?
                 AND oi.sku IS NOT NULL
                 AND TRIM(oi.sku)<>''
                 AND c.sku IS NULL""",
            (shop_id,),
        ).fetchall()

        inherited = 0
        for row in rows:
            if self.inherit_model_cost(
                shop_id,
                row["sku"],
                row["name"],
                row["category"],
            ):
                inherited += 1

        return inherited

    def upsert_single_cost(self, shop_id, sku, cost_price):
        """Сохраняет себестоимость один раз для SKU.

        Если строка уже существует (например, была загружена CSV),
        меняется только cost_price, остальные параметры сохраняются.
        Для нового SKU остальные расходы пока ставятся 0 и могут быть
        позже уточнены через CSV.
        """
        now = datetime.now(timezone.utc).isoformat()
        existing = self.cost(shop_id, sku)
        if existing:
            self.conn.execute(
                """UPDATE costs
                   SET cost_price=?, updated_at=?
                   WHERE shop_id=? AND sku=?""",
                (float(cost_price), now, shop_id, str(sku)),
            )
        else:
            self.conn.execute(
                """INSERT INTO costs
                   (shop_id,sku,cost_price,commission_rate,tax_rate,
                    acquiring_rate,packaging_cost,other_cost,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (shop_id, str(sku), float(cost_price), 0, 0, 0, 0, 0, now),
            )
        self.conn.commit()

    def ambiguous_skus(self, shop_id):
        rows = self.conn.execute(
            "SELECT DISTINCT sku FROM order_items WHERE shop_id=? AND sku IS NOT NULL", (shop_id,),
        ).fetchall()
        return [row["sku"] for row in rows if self.sku_is_ambiguous(shop_id, row["sku"])]

    def catalog_products(self, shop_id):
        """One entry per real Kaspi item, not merchant SKU (SKU can be reused)."""
        rows = self.conn.execute(
            """SELECT oi.rowid AS item_rowid, oi.*, o.creation_ms AS last_order_ms
               FROM order_items oi JOIN orders o
                 ON o.shop_id=oi.shop_id AND o.id=oi.order_id
               WHERE oi.shop_id=? AND oi.sku IS NOT NULL AND TRIM(oi.sku)<>''
               ORDER BY o.creation_ms DESC, oi.rowid DESC""", (shop_id,),
        ).fetchall()
        groups = {}
        for item in rows:
            identity = product_cost_identity(item)
            if identity not in groups:
                cost = self.cost_for_item(shop_id, item)
                result = {
                    "item_rowid": item["item_rowid"], "sku": item["sku"],
                    "name": item["name"] or "Без названия",
                    "kaspi_product_code": item["kaspi_product_code"],
                    "category": item["category"], "image_url": item["image_url"],
                    "last_order_ms": item["last_order_ms"],
                    "order_count": 0,
                    "sale_price": (float(item["total_price"] or 0)/float(item["quantity"])
                                   if float(item["quantity"] or 0) > 0 else None),
                    "cost_price": float(cost["cost_price"]) if cost else None,
                }
                for field in ("commission_rate", "tax_rate", "acquiring_rate", "packaging_cost", "other_cost"):
                    result[field] = float(cost[field] or 0) if cost else 0
                groups[identity] = (result, set())
            result, orders = groups[identity]
            orders.add(item["order_id"])
            result["order_count"] = len(orders)
        return [entry[0] for entry in groups.values()]

    def upsert_cost_partial(
        self,
        shop_id,
        sku,
        cost_price=None,
        commission_rate=None,
        tax_rate=None,
        acquiring_rate=None,
        packaging_cost=None,
        other_cost=None,
    ):
        """Update exact SKU only. Blank Excel cells preserve existing values."""
        sku = str(sku or "").strip()
        if not sku:
            return False

        if self.sku_is_ambiguous(shop_id, sku):
            # Legacy XLSX imports cannot disambiguate product identity by SKU.
            # Do not attach a hat price to a stole with the same article.
            return False
        existing = self.cost(shop_id, sku)

        def num(value, fallback):
            if value is None:
                return fallback
            if isinstance(value, str):
                value = value.strip().replace(" ", "").replace(",", ".")
                if value == "":
                    return fallback
            return float(value)

        if existing:
            cp = num(cost_price, float(existing["cost_price"]))
            cr = num(commission_rate, float(existing["commission_rate"]))
            tr = num(tax_rate, float(existing["tax_rate"]))
            ar = num(acquiring_rate, float(existing["acquiring_rate"]))
            pc = num(packaging_cost, float(existing["packaging_cost"]))
            oc = num(other_cost, float(existing["other_cost"]))
        else:
            if cost_price is None or str(cost_price).strip() == "":
                return False
            cp = num(cost_price, 0)
            cr = num(commission_rate, 0)
            tr = num(tax_rate, 0)
            ar = num(acquiring_rate, 0)
            pc = num(packaging_cost, 0)
            oc = num(other_cost, 0)

        now = datetime.now(timezone.utc).isoformat()

        self.conn.execute(
            """INSERT INTO costs
               (shop_id,sku,cost_price,commission_rate,tax_rate,
                acquiring_rate,packaging_cost,other_cost,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(shop_id,sku) DO UPDATE SET
                 cost_price=excluded.cost_price,
                 commission_rate=excluded.commission_rate,
                 tax_rate=excluded.tax_rate,
                 acquiring_rate=excluded.acquiring_rate,
                 packaging_cost=excluded.packaging_cost,
                 other_cost=excluded.other_cost,
                 updated_at=excluded.updated_at""",
            (
                shop_id,
                sku,
                cp,
                cr,
                tr,
                ar,
                pc,
                oc,
                now,
            ),
        )
        self.conn.commit()
        return True

    def filled_cost_count(self, shop_id):
        return sum(1 for row in self.catalog_products(shop_id) if row["cost_price"] is not None)

    def filled_cost_examples(self, shop_id, limit=8, offset=0):
        rows = [row for row in self.catalog_products(shop_id) if row["cost_price"] is not None]
        return rows[int(offset):int(offset)+int(limit)]

    def _missing_cost_products(self, shop_id, start_ms=None, end_ms=None, limit=None):
        period = ""
        params = [shop_id]
        if start_ms is not None and end_ms is not None:
            period = (" AND ((o.creation_ms>=? AND o.creation_ms<?) "
                      "OR (o.completion_ms>=? AND o.completion_ms<?))")
            params.extend([int(start_ms), int(end_ms), int(start_ms), int(end_ms)])
        rows = self.conn.execute(
            """SELECT oi.rowid AS item_rowid, oi.*, o.code AS order_code,
                      o.creation_ms AS last_order_ms
               FROM order_items oi JOIN orders o
               ON o.shop_id=oi.shop_id AND o.id=oi.order_id
               WHERE oi.shop_id=? AND oi.sku IS NOT NULL AND TRIM(oi.sku)<>''"""
            + period + " ORDER BY o.creation_ms DESC, oi.rowid DESC", params,
        ).fetchall()
        seen = set()
        result = []
        for item in rows:
            identity = product_cost_identity(item)
            if identity in seen:
                continue
            seen.add(identity)
            if self.cost_for_item(shop_id, item):
                continue
            result.append({
                "item_rowid": item["item_rowid"], "sku": item["sku"],
                "name": item["name"], "category": item["category"],
                "kaspi_product_code": item["kaspi_product_code"],
                "order_code": item["order_code"],
                "sample_price": float(item["total_price"] or 0) /
                    float(item["quantity"] or 1),
                "order_count": 1, "last_order_ms": item["last_order_ms"],
            })
            if limit and len(result) >= limit:
                break
        return result

    def missing_cost_count(self, shop_id):
        return len(self._missing_cost_products(shop_id))

    def missing_cost_count_for_report_period(self, shop_id, start_ms, end_ms):
        return len(self._missing_cost_products(shop_id, start_ms, end_ms))

    def missing_cost_examples_for_report_period(self, shop_id, start_ms, end_ms, limit=12):
        return self._missing_cost_products(shop_id, start_ms, end_ms, limit)

    def missing_cost_examples(self, shop_id, limit=12):
        return self._missing_cost_products(shop_id, limit=limit)

    def item_by_rowid(self, shop_id, rowid):
        return self.conn.execute(
            """SELECT oi.rowid AS item_rowid, oi.*, o.code AS order_code
               FROM order_items oi
               JOIN orders o
                 ON o.shop_id=oi.shop_id AND o.id=oi.order_id
               WHERE oi.shop_id=? AND oi.rowid=?""",
            (shop_id, int(rowid)),
        ).fetchone()

    def set_image_for_sku(self, shop_id, sku, image_url):
        if not image_url:
            return
        self.conn.execute(
            """UPDATE order_items
               SET image_url=?
               WHERE shop_id=? AND sku=?""",
            (image_url, shop_id, str(sku)),
        )
        self.conn.commit()

    def image_for_sku(self, shop_id, sku):
        row = self.conn.execute(
            """SELECT image_url
               FROM order_items
               WHERE shop_id=? AND sku=?
                 AND image_url IS NOT NULL
                 AND TRIM(image_url)<>''
               ORDER BY rowid DESC
               LIMIT 1""",
            (shop_id, str(sku)),
        ).fetchone()
        return row["image_url"] if row else None

    def has_items(self, shop_id, order_id):
        return bool(
            self.conn.execute(
                """SELECT 1 FROM order_items
                   WHERE shop_id=? AND order_id=?
                   LIMIT 1""",
                (shop_id, order_id),
            ).fetchone()
        )

    def items(self, shop_id, order_id):
        return self.conn.execute(
            """SELECT * FROM order_items
               WHERE shop_id=? AND order_id=?""",
            (shop_id, order_id),
        ).fetchall()

    def created_between(self, shop_id, start_ms, end_ms):
        return self.conn.execute(
            """SELECT * FROM orders
               WHERE shop_id=? AND creation_ms>=? AND creation_ms<?
               ORDER BY creation_ms""",
            (shop_id, start_ms, end_ms),
        ).fetchall()

    def completed_between(self, shop_id, start_ms, end_ms):
        return self.conn.execute(
            """SELECT * FROM orders
               WHERE shop_id=?
                 AND completion_ms IS NOT NULL
                 AND completion_ms>=? AND completion_ms<?
               ORDER BY completion_ms""",
            (shop_id, start_ms, end_ms),
        ).fetchall()

    def status_stats(self, shop_id):
        row = self.conn.execute(
            """SELECT
               (SELECT COUNT(*) FROM orders WHERE shop_id=?) AS orders_count,
               (SELECT COUNT(*) FROM order_items WHERE shop_id=?) AS item_count,
               (SELECT COUNT(*) FROM costs WHERE shop_id=?) AS cost_count,
               (SELECT MAX(last_seen_at) FROM orders WHERE shop_id=?) AS last_seen""",
            (shop_id, shop_id, shop_id, shop_id),
        ).fetchone()
        return dict(row)

    def start_sync(self, shop_id, from_ms, to_ms):
        cur = self.conn.execute(
            """INSERT INTO sync_log(shop_id,started_at,from_ms,to_ms)
               VALUES(?,?,?,?)""",
            (
                shop_id,
                datetime.now(timezone.utc).isoformat(),
                from_ms,
                to_ms,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_sync(self, log_id, count, error=None):
        self.conn.execute(
            """UPDATE sync_log
               SET finished_at=?,orders_seen=?,error=?
               WHERE id=?""",
            (
                datetime.now(timezone.utc).isoformat(),
                count,
                error,
                log_id,
            ),
        )
        self.conn.commit()


DB = Database()

# shop_id values currently being synchronized.
# Prevents manual + scheduled sync from running twice for the same shop.
RUNNING_SYNCS = set()
RUNNING_MARKETING_SYNCS = set()


class KaspiAPI:
    def __init__(self, token):
        self.token = token

    def headers(self):
        return {
            "Content-Type": "application/vnd.api+json",
            "X-Auth-Token": self.token,
            "User-Agent": "KaspiProfitBot/0.2",
        }

    async def get(self, path, params=None):
        # Kaspi can occasionally respond slowly. Retry only transient
        # network errors; HTTP 4xx/5xx still propagate immediately.
        last_exc = None

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        connect=20.0,
                        read=60.0,
                        write=30.0,
                        pool=30.0,
                    ),
                    headers=self.headers(),
                ) as client:
                    response = await client.get(
                        KASPI_BASE + path,
                        params=params,
                    )
                    response.raise_for_status()
                    return response.json()

            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.PoolTimeout,
            ) as exc:
                last_exc = exc
                if attempt >= 2:
                    raise
                await asyncio.sleep(2 * (attempt + 1))

        if last_exc:
            raise last_exc

    async def validate(self):
        now = datetime.now(TZ)
        start = now - timedelta(days=1)
        params = {
            "page[number]": 0,
            "page[size]": 1,
            "filter[orders][creationDate][$ge]": int(start.timestamp() * 1000),
            "filter[orders][creationDate][$le]": int(now.timestamp() * 1000),
        }
        await self.get("/orders", params=params)
        return True

    async def list_orders_for_state(self, from_ms, to_ms, state):
        page = 0
        result = []

        while True:
            payload = await self.get(
                "/orders",
                {
                    "page[number]": page,
                    "page[size]": 100,
                    "filter[orders][state]": state,
                    "filter[orders][creationDate][$ge]": from_ms,
                    "filter[orders][creationDate][$le]": to_ms - 1,
                },
            )

            chunk = payload.get("data") or []
            result.extend(chunk)

            meta = payload.get("meta") or {}
            page_count = meta.get("pageCount")
            page += 1

            if page_count is not None:
                if page >= int(page_count):
                    break
            elif len(chunk) < 100:
                break

            if not chunk:
                break

        return result

    async def list_orders(self, from_ms, to_ms, with_stats=False):
        by_id = {}
        stats = {}

        for state in ORDER_STATES:
            state_orders = await self.list_orders_for_state(
                from_ms,
                to_ms,
                state,
            )
            stats[state] = len(state_orders)

            for order in state_orders:
                order_id = order.get("id")
                if order_id:
                    by_id[order_id] = order

        result = list(by_id.values())

        if with_stats:
            return result, stats

        return result

    async def entries(self, order_id):
        payload = await self.get(f"/orders/{order_id}/entries")
        return payload.get("data") or []

    async def product(self, entry_id):
        try:
            return await self.get(
                f"/orderentries/{entry_id}/product"
            )
        except httpx.HTTPStatusError:
            return None

    async def merchant_product(self, master_product_id):
        if not master_product_id:
            return None
        try:
            return await self.get(
                f"/masterproducts/{master_product_id}/merchantProduct"
            )
        except httpx.HTTPStatusError:
            return None




KASPI_PUBLIC_SEARCH_URL = "https://kaspi.kz/yml/product-view/pl/results"
KASPI_PUBLIC_CITY_CODE = "750000000"


def is_kaspi_product_image_url(value):
    if not value or not isinstance(value, str):
        return False

    low = value.lower().replace("\\/", "/")

    if not low.startswith(("http://", "https://", "//")):
        return False

    if any(bad in low for bad in (
        "logo", "icon", "sprite", "banner", "favicon",
        "placeholder", "empty-image", "no-image",
    )):
        return False

    # Current Kaspi product-photo CDN format observed in real cards.
    if "resources.cdn-kaspi.kz/img/m/p/" in low:
        return True

    # Keep compatibility with older Kaspi CDN product image layout.
    if (
        "cdn-kaspi.kz/shop/medias/sys_master/images/" in low
        or "resources.cdn-kaspi.kz/shop/medias/sys_master/images/" in low
    ):
        return True

    return False


def gallery_medium_url(value):
    """Normalize a Kaspi image URL to a Telegram-friendly preview size."""
    if not value:
        return None

    value = html.unescape(str(value)).replace("\\/", "/").strip()

    if value.startswith("//"):
        value = "https:" + value

    if not is_kaspi_product_image_url(value):
        return None

    # The modern CDN supports format=gallery-medium.
    if "resources.cdn-kaspi.kz/img/m/p/" in value.lower():
        base, sep, query = value.partition("?")

        params = []
        if query:
            for part in query.split("&"):
                if not part:
                    continue
                if part.lower().startswith("format="):
                    continue
                params.append(part)

        params.append("format=gallery-medium")
        return base + "?" + "&".join(params)

    return value


def _walk_strings(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)
    elif isinstance(value, str):
        yield value


def _exact_public_product(items, code):
    code = str(code).strip()

    # Strong exact identifiers first.
    for item in items:
        if not isinstance(item, dict):
            continue

        candidates = (
            item.get("id"),
            item.get("configSku"),
            item.get("sku"),
            item.get("productId"),
            item.get("masterProductId"),
        )

        if any(str(v).strip() == code for v in candidates if v is not None):
            return item

    # Then exact code in the product link.
    for item in items:
        if not isinstance(item, dict):
            continue

        link = str(
            item.get("shopLink")
            or item.get("link")
            or item.get("url")
            or ""
        ).replace("\\/", "/")

        if (
            f"-{code}/" in link
            or f"/{code}/" in link
            or link.rstrip("/").endswith(code)
        ):
            return item

    # Some responses expose base product codes as an array.
    for item in items:
        if not isinstance(item, dict):
            continue

        base_codes = item.get("baseProductCodes") or []
        if isinstance(base_codes, list):
            if any(str(v).strip() == code for v in base_codes):
                return item

    return None


def _product_image_from_public_item(item):
    if not isinstance(item, dict):
        return None

    # Prefer obvious image fields when present.
    preferred_keys = (
        "image",
        "imageUrl",
        "imageURL",
        "picture",
        "pictureUrl",
        "preview",
        "previewImage",
        "mainImage",
        "primaryImage",
        "images",
        "gallery",
    )

    for key in preferred_keys:
        if key not in item:
            continue

        for candidate in _walk_strings(item.get(key)):
            normalized = gallery_medium_url(candidate)
            if normalized:
                return normalized

    # Fallback: recursively inspect this exact product object only.
    # We never inspect the whole search page/result globally, which prevents
    # grabbing a Kaspi logo or banner.
    for candidate in _walk_strings(item):
        normalized = gallery_medium_url(candidate)
        if normalized:
            return normalized

    return None


async def resolve_public_product_image(kaspi_product_code, product_name=None):
    """
    Search Kaspi's public product JSON endpoint by exact Kaspi product code,
    choose the exact matching item, and extract only its product-photo CDN URL.
    """
    if not kaspi_product_code:
        return None

    code = str(kaspi_product_code).strip()

    params = {
        "text": code,
        "c": KASPI_PUBLIC_CITY_CODE,
        "sort": "relevance",
        "page": "0",
    }

    referer = (
        "https://kaspi.kz/shop/search/"
        f"?text={quote_plus(code)}&c={KASPI_PUBLIC_CITY_CODE}"
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        "Referer": referer,
        "X-KS-City": KASPI_PUBLIC_CITY_CODE,
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=12,
                read=30,
                write=12,
                pool=12,
            ),
            headers=headers,
            follow_redirects=True,
        ) as client:
            response = await client.get(
                KASPI_PUBLIC_SEARCH_URL,
                params=params,
            )
            response.raise_for_status()
            payload = response.json()

        items = payload.get("data") or []
        if not isinstance(items, list):
            print(
                "Kaspi public image search returned unexpected data "
                f"for code={code}"
            )
            return None

        exact = _exact_public_product(items, code)

        if exact is None:
            print(
                "Kaspi public image search: exact product not found "
                f"code={code}, returned={len(items)}"
            )
            return None

        image_url = _product_image_from_public_item(exact)

        if image_url:
            print(
                "Kaspi product image resolved "
                f"code={code}: {image_url}"
            )
            return image_url

        print(
            "Kaspi public product found but image URL absent "
            f"code={code}"
        )

    except Exception as exc:
        print(
            "Kaspi public product image lookup failed "
            f"code={code}: {repr(exc)}"
        )

    return None


async def download_product_preview(image_url):
    image_url = gallery_medium_url(image_url)

    if not image_url:
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0 Safari/537.36"
        ),
        "Referer": "https://kaspi.kz/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10,
                read=30,
                write=10,
                pool=10,
            ),
            headers=headers,
            follow_redirects=True,
        ) as client:
            response = await client.get(image_url)
            response.raise_for_status()

        content_type = (
            response.headers.get("content-type", "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )

        if not content_type.startswith("image/"):
            print(
                "Kaspi image URL did not return an image: "
                f"{content_type} {image_url}"
            )
            return None

        data = response.content

        if len(data) < 1000:
            print(
                "Kaspi image response too small: "
                f"{len(data)} bytes {image_url}"
            )
            return None

        bio = io.BytesIO(data)

        if "png" in content_type:
            bio.name = "product.png"
        elif "webp" in content_type:
            bio.name = "product.webp"
        else:
            bio.name = "product.jpeg"

        bio.seek(0)
        return bio

    except Exception as exc:
        print(
            "Product preview download failed "
            f"url={image_url}: {repr(exc)}"
        )
        return None


def shop_token(shop):
    try:
        return decrypt_token(shop["token_encrypted"])
    except InvalidToken:
        raise RuntimeError(
            "Не удалось расшифровать токен. "
            "Проверьте data/secret.key или замените токен магазина."
        )


async def sync_shop(shop, days=None):
    """
    Kaspi ограничивает один запрос по creationDate коротким периодом.
    Поэтому длинное окно (например 90 дней) разбиваем на безопасные
    куски по 13 дней и последовательно обновляем базу.
    """
    days = days or ROLLING_SYNC_DAYS
    token = shop_token(shop)
    api = KaspiAPI(token)

    now = datetime.now(TZ)
    start = (now - timedelta(days=days)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    end = now + timedelta(minutes=2)

    overall_from_ms = int(start.timestamp() * 1000)
    overall_to_ms = int(end.timestamp() * 1000)

    log_id = DB.start_sync(
        shop["id"],
        overall_from_ms,
        overall_to_ms,
    )

    count = 0
    chunk_days = 13

    try:
        # Most recent data first. This makes today/yesterday available
        # immediately even while a long first 90-day backfill continues.
        chunk_end = end

        while chunk_end > start:
            chunk_start = max(
                start,
                chunk_end - timedelta(days=chunk_days),
            )

            from_ms = int(chunk_start.timestamp() * 1000)
            to_ms = int(chunk_end.timestamp() * 1000)

            orders, state_stats = await api.list_orders(
                from_ms,
                to_ms,
                with_stats=True,
            )

            print(
                "Full sync states "
                f"{chunk_start.date()}..{(chunk_end - timedelta(milliseconds=1)).date()} "
                + " ".join(
                    f"{state}={state_stats.get(state, 0)}"
                    for state in ORDER_STATES
                )
            )

            for order in orders:
                order_id = order["id"]

                # Сначала смотрим, был ли заказ уже в базе.
                # Статус/даты обновляем всегда, чтобы ловить
                # поздние отмены и возвраты.
                had_items = DB.has_items(
                    shop["id"],
                    order_id,
                )

                DB.upsert_order(
                    shop["id"],
                    order,
                )
                count += 1

                # Состав заказа и SKU обычно не меняются.
                # Не дергаем Kaspi повторно для каждого старого заказа
                # при каждой 10-минутной синхронизации.
                if had_items:
                    continue

                entries = await api.entries(order_id)

                for entry in entries:
                    product = await api.product(entry["id"])
                    master_id = None

                    if product and product.get("data"):
                        master_id = product["data"].get("id")

                    merchant = (
                        await api.merchant_product(master_id)
                        if master_id
                        else None
                    )

                    DB.upsert_item(
                        shop["id"],
                        order_id,
                        entry,
                        product,
                        merchant,
                    )

            chunk_end = chunk_start

        DB.finish_sync(log_id, count)
        return count

    except Exception as exc:
        DB.finish_sync(
            log_id,
            count,
            str(exc),
        )
        raise


async def sync_shop_date_range(shop, start_date, end_date):
    """Fast refresh of orders CREATED in the selected date range."""
    token = shop_token(shop)
    api = KaspiAPI(token)

    start_dt, end_dt = business_day_bounds_dt(
        start_date,
        end_date,
    )

    count = 0
    chunk_start = start_dt

    while chunk_start < end_dt:
        chunk_end = min(
            chunk_start + timedelta(days=13),
            end_dt,
        )

        orders, state_stats = await api.list_orders(
            int(chunk_start.timestamp() * 1000),
            int(chunk_end.timestamp() * 1000),
            with_stats=True,
        )

        print(
            "Quick sync states "
            f"{chunk_start.date()}..{(chunk_end - timedelta(milliseconds=1)).date()} "
            + " ".join(
                f"{state}={state_stats.get(state, 0)}"
                for state in ORDER_STATES
            )
        )

        for order in orders:
            order_id = order["id"]
            had_items = DB.has_items(shop["id"], order_id)

            DB.upsert_order(shop["id"], order)
            count += 1

            if had_items:
                continue

            entries = await api.entries(order_id)

            for entry in entries:
                product = await api.product(entry["id"])
                master_id = None

                if product and product.get("data"):
                    master_id = product["data"].get("id")

                merchant = (
                    await api.merchant_product(master_id)
                    if master_id
                    else None
                )

                DB.upsert_item(
                    shop["id"],
                    order_id,
                    entry,
                    product,
                    merchant,
                )

        chunk_start = chunk_end

    return count


ACCESSORY_CATEGORY_HINTS = (
    "головные уборы",
    "шапки",
    "шарфы",
    "платки",
    "перчатки",
    "варежки",
)


def effective_financial_rates(shop_id, item, cost):
    """
    SKU-level non-zero values override shop defaults.
    For the user's current accessory categories, official 2026 tariff
    with VAT is 15.5%, so category matching can supply it if SKU rate is empty.
    """
    settings = DB.financial_settings(shop_id)

    category = str(item["category"] or "").lower()

    category_commission = None
    if any(hint in category for hint in ACCESSORY_CATEGORY_HINTS):
        category_commission = 15.5

    sku_commission = float(cost["commission_rate"] or 0)
    sku_tax = float(cost["tax_rate"] or 0)
    sku_acquiring = float(cost["acquiring_rate"] or 0)

    commission_rate = (
        sku_commission
        if sku_commission > 0
        else (
            category_commission
            if category_commission is not None
            else float(settings["default_commission_rate"] or 0)
        )
    )

    tax_rate = (
        sku_tax
        if sku_tax > 0
        else float(settings["default_tax_rate"] or 0)
    )

    acquiring_rate = (
        sku_acquiring
        if sku_acquiring > 0
        else float(settings["default_acquiring_rate"] or 0)
    )

    return {
        "commission_rate": commission_rate,
        "tax_rate": tax_rate,
        "acquiring_rate": acquiring_rate,
    }



def item_quantity(item):
    """Kaspi entries carry quantity; a missing value should not become zero COGS."""
    qty = float(item["quantity"] or 0)
    return qty if qty > 0 else 0.0


def item_unit_price(item):
    """Unit sale price is derived from actual line amount and number of units."""
    qty = item_quantity(item)
    return float(item["total_price"] or 0) / qty if qty > 0 else 0.0


def quantity_label(qty):
    return str(int(qty)) if qty.is_integer() else f"{qty:g}"


def cost_card_price_lines(item):
    qty = item_quantity(item)
    revenue = float(item["total_price"] or 0)
    if qty <= 0:
        return (
            "⚠️ Kaspi не передал количество товара — проверьте состав заказа.\n"
            f"Сумма позиции: <b>{money(revenue)}</b>\n"
            "Введите себестоимость ОДНОЙ штуки. Прибыль по этой позиции "
            "не будет рассчитана до получения количества из Kaspi."
        )
    return (
        f"Количество: <b>{quantity_label(qty)} шт.</b>\n"
        f"Цена продажи за 1 шт.: <b>{money(item_unit_price(item))}</b>\n"
        f"Сумма позиции ({quantity_label(qty)} шт.): <b>{money(revenue)}</b>\n"
        "<b>Введите себестоимость только ОДНОЙ штуки.</b>\n"
        f"Себестоимость всей позиции будет умножена на {quantity_label(qty)}."
    )


def return_delivery_loss(shop_id, orders):
    """Delivery is charged to the seller even if a completed order is returned."""
    total, unknown = 0.0, 0
    for order in orders:
        info = order_delivery_info(order, list(DB.items(shop_id, order["id"])))
        if info["seller_known"]:
            total += max(0.0, float(info["seller_cost"] or 0))
        else:
            unknown += 1
    return total, unknown


def returned_order_marketing_spend(shop_id, order_code):
    """Exact order-code matching only; SKU-only ad rows are not reliable attribution."""
    if not str(order_code or "").strip():
        return 0.0, False
    row = DB.conn.execute(
        """SELECT COALESCE(SUM(amount), 0) AS amount, COUNT(*) AS matches
           FROM marketing_report_rows
           WHERE shop_id=? AND TRIM(order_code)=? AND section_key IN ('external','goods')""",
        (shop_id, str(order_code).strip()),
    ).fetchone()
    return float(row["amount"] or 0), bool(row["matches"])


def return_loss_summary(shop_id, orders):
    """Physical returns restore sale inventory/commissions, not delivery/ad spend."""
    summary = {
        "orders": len(orders), "units": 0.0, "refunded_revenue": 0.0,
        "delivery": 0.0, "delivery_unknown": 0,
        "marketing_attributed": 0.0, "ads_unattributed_orders": 0,
        "restocked_units": 0.0, "unconfirmed_warehouse_orders": 0,
        "commission_reversed_estimate": 0.0,
        "stock_value_recovered": 0.0, "missing_cost_units": 0.0,
    }
    for order in orders:
        items = list(DB.items(shop_id, order["id"]))
        summary["refunded_revenue"] += float(order["total_price"] or 0)
        warehouse = bool(order["returned_to_warehouse"])
        if not warehouse:
            summary["unconfirmed_warehouse_orders"] += 1
        for item in items:
            qty = item_quantity(item)
            summary["units"] += qty
            if warehouse:
                summary["restocked_units"] += qty
            cost = DB.cost_for_item(shop_id, item)
            if not cost:
                summary["missing_cost_units"] += qty
                continue
            revenue = float(item["total_price"] or 0)
            summary["commission_reversed_estimate"] += (
                revenue * effective_financial_rates(shop_id, item, cost)["commission_rate"] / 100.0
            )
            if warehouse:
                summary["stock_value_recovered"] += float(cost["cost_price"]) * qty
        info = order_delivery_info(order, items)
        if info["seller_known"]:
            summary["delivery"] += max(0.0, float(info["seller_cost"] or 0))
        else:
            summary["delivery_unknown"] += 1
        ad, matched = returned_order_marketing_spend(shop_id, order["code"])
        summary["marketing_attributed"] += ad
        if not matched:
            summary["ads_unattributed_orders"] += 1
    summary["confirmed_unrecoverable"] = (
        summary["delivery"] + summary["marketing_attributed"]
    )
    return summary


def order_profit(shop_id, order):
    items = DB.items(shop_id, order["id"])

    if not items:
        return None, {"(нет состава заказа)"}

    total = 0.0
    missing = set()

    for item in items:
        cost = DB.cost_for_item(shop_id, item)

        if not cost:
            missing.add(
                item["sku"] or "(SKU не определён)"
            )
            continue

        revenue = float(item["total_price"] or 0)
        qty = item_quantity(item)
        if qty <= 0:
            missing.add(f"{item['sku']} — количество не определено")
            continue

        cogs = float(cost["cost_price"]) * qty
        rates = effective_financial_rates(
            shop_id,
            item,
            cost,
        )
        commission = (
            revenue
            * rates["commission_rate"]
            / 100.0
        )
        tax = (
            revenue
            * rates["tax_rate"]
            / 100.0
        )
        acquiring = (
            revenue
            * rates["acquiring_rate"]
            / 100.0
        )
        packaging = (
            float(cost["packaging_cost"])
            * qty
        )
        other = (
            float(cost["other_cost"])
            * qty
        )

        total += (
            revenue
            - cogs
            - commission
            - tax
            - acquiring
            - packaging
            - other
        )

    if missing:
        return None, missing

    delivery_info = order_delivery_info(
        order,
        list(items),
    )

    # Deduct only the seller-specific delivery field.
    # Do NOT substitute order.deliveryCost or entry.deliveryCost.
    if delivery_info["seller_known"]:
        total -= float(
            delivery_info["seller_cost"] or 0
        )

    return total, set()


def aggregate(shop_id, orders):
    result = {
        "orders": 0,
        "revenue": 0.0,
        "profit": 0.0,
        "missing_orders": 0,
        "missing_skus": set(),
    }

    for order in orders:
        result["orders"] += 1
        result["revenue"] += float(
            order["total_price"] or 0
        )

        profit, missing = order_profit(
            shop_id,
            order,
        )

        if profit is None:
            result["missing_orders"] += 1
            result["missing_skus"].update(missing)
        else:
            result["profit"] += profit

    return result


def current_business_date(now=None):
    """
    Accounting day is 11:00 -> 11:00 Asia/Almaty.
    Before 11:00, "today" is still the business day that started yesterday.
    """
    now = now or datetime.now(TZ)
    boundary = now.replace(
        hour=BUSINESS_DAY_START_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    if now < boundary:
        return now.date() - timedelta(days=1)
    return now.date()


def business_day_bounds_dt(start_date, end_date):
    start = datetime.combine(
        start_date,
        time(
            hour=BUSINESS_DAY_START_HOUR
        ),
        TZ,
    )
    end = datetime.combine(
        end_date + timedelta(days=1),
        time(
            hour=BUSINESS_DAY_START_HOUR
        ),
        TZ,
    )
    return start, end


def date_bounds_ms(start_date, end_date):
    start, end = business_day_bounds_dt(
        start_date,
        end_date,
    )
    return (
        int(start.timestamp() * 1000),
        int(end.timestamp() * 1000),
    )


def business_period_label(start_date, end_date):
    start, end = business_day_bounds_dt(
        start_date,
        end_date,
    )
    if start_date == end_date:
        return (
            f"{start_date:%d.%m.%Y} "
            f"({start:%H:%M} → {end:%d.%m %H:%M})"
        )
    return (
        f"{start:%d.%m.%Y %H:%M} → "
        f"{end:%d.%m.%Y %H:%M}"
    )


MARKETING_BREAKDOWN_ORDER = (
    "bonus_purchase",
    "external",
    "goods",
    "bonus_review",
)

MARKETING_ACQUISITION_SECTIONS = (
    "external",
    "goods",
)

MARKETING_ISSUED_BONUS_SECTIONS = (
    "bonus_purchase",
    "bonus_review",
)


def marketing_section_totals_for_period(
    shop_id,
    start_date,
    end_date,
):
    totals = {
        key: 0.0
        for key in MARKETING_BREAKDOWN_ORDER
    }

    rows = DB.marketing_rows_for_period(
        shop_id,
        start_date,
        end_date,
    )

    for row in rows:
        section_key = str(
            row["section_key"]
            or ""
        ).strip()

        if section_key in totals:
            totals[section_key] += float(
                row["amount"]
                or 0
            )

    return totals


def marketing_breakdown_plain_lines(
    totals,
    indent="",
):
    return [
        (
            f"{indent}{MARKETING_REPORT_SECTIONS[key]}: "
            f"{money(float(totals.get(key, 0) or 0))}"
        )
        for key in MARKETING_BREAKDOWN_ORDER
    ]


def marketing_breakdown_html_lines(
    totals,
):
    return [
        (
            f"  • {MARKETING_REPORT_SECTIONS[key]}: "
            f"<b>{money(float(totals.get(key, 0) or 0))}</b>"
        )
        for key in MARKETING_BREAKDOWN_ORDER
    ]


def render_report(shop, start_date, end_date):
    shop_id = shop["id"]
    start_ms, end_ms = date_bounds_ms(
        start_date,
        end_date,
    )

    created = list(
        DB.created_between(
            shop_id,
            start_ms,
            end_ms,
        )
    )
    completed = list(
        DB.completed_between(
            shop_id,
            start_ms,
            end_ms,
        )
    )

    all_created = aggregate(shop_id, created)
    active = aggregate(
        shop_id,
        [o for o in created if o["status"] in ACTIVE_EXPECTED],
    )
    cancelled = aggregate(
        shop_id,
        [o for o in created if o["status"] in CANCELLED],
    )
    cancelling = aggregate(
        shop_id,
        [o for o in created if o["status"] in CANCELLING],
    )
    actual = aggregate(
        shop_id,
        [o for o in completed if o["status"] in COMPLETED],
    )
    returned = aggregate(
        shop_id,
        [o for o in completed if o["status"] in RETURNED],
    )
    return_requested = aggregate(
        shop_id,
        [o for o in completed if o["status"] in RETURN_REQUESTED],
    )
    returned_orders = [o for o in completed if o["status"] in RETURNED]
    returned_shipping_loss, returned_shipping_unknown = return_delivery_loss(
        shop_id, returned_orders
    )

    title = business_period_label(
        start_date,
        end_date,
    )

    marketing_section_totals = (
        marketing_section_totals_for_period(
            shop_id,
            start_date,
            end_date,
        )
    )

    acquisition_spend = sum(
        float(
            marketing_section_totals.get(
                key,
                0,
            )
            or 0
        )
        for key in MARKETING_ACQUISITION_SECTIONS
    )

    issued_bonus_spend = sum(
        float(
            marketing_section_totals.get(
                key,
                0,
            )
            or 0
        )
        for key in MARKETING_ISSUED_BONUS_SECTIONS
    )

    total_marketing_spend = (
        acquisition_spend
        + issued_bonus_spend
    )

    lines = [
        f"🏪 <b>{shop['name']}</b>",
        f"📊 <b>{title}</b>",
        "",
        "<b>ЗАКАЗАНО</b>",
        f"Заказов: <b>{all_created['orders']}</b>",
        f"Сумма: <b>{money(all_created['revenue'])}</b>",
        (
            f"Внешняя реклама 11→11: "
            f"<b>{money(marketing_section_totals.get('external', 0))}</b>"
        ),
        (
            f"Реклама товаров 11→11: "
            f"<b>{money(marketing_section_totals.get('goods', 0))}</b>"
        ),
        (
            f"Фактически потрачено рекламного бюджета: "
            f"<b>{money(acquisition_spend)}</b>"
        ),
        "",
        "<b>ОЖИДАЕТСЯ / В РАБОТЕ</b>",
        f"Активных: <b>{active['orders']}</b>",
        f"Сумма: <b>{money(active['revenue'])}</b>",
    ]

    if active["missing_orders"]:
        lines.append(
            f"Ожидаемая прибыль: <b>{money(active['profit'])}</b> "
            f"+ {active['missing_orders']} без себестоимости"
        )
    else:
        lines.append(
            f"Ожидаемая прибыль: <b>{money(active['profit'])}</b>"
        )

    lines += [
        "",
        "<b>ФАКТИЧЕСКИ ВЫДАНО</b>",
        f"Выдано и сейчас не возвращено: <b>{actual['orders']}</b>",
        f"Выручка: <b>{money(actual['revenue'])}</b>",
    ]

    if actual["missing_orders"]:
        lines.append(
            f"Фактическая прибыль: <b>{money(actual['profit'])}</b> "
            f"+ {actual['missing_orders']} без себестоимости"
        )
    else:
        lines.append(
            f"Фактическая прибыль: <b>{money(actual['profit'])}</b>"
        )

    lines += [
        (
            f"Бонус от продавца: "
            f"<b>{money(marketing_section_totals.get('bonus_purchase', 0))}</b>"
        ),
        (
            f"Бонус за отзыв: "
            f"<b>{money(marketing_section_totals.get('bonus_review', 0))}</b>"
        ),
        (
            f"Бонусы по факту выдачи: "
            f"<b>{money(issued_bonus_spend)}</b>"
        ),
        (
            f"Фактическая прибыль после бонусов: "
            f"<b>{money(actual['profit'] - issued_bonus_spend)}</b>"
        ),
    ]

    lines += [
        "",
        (
            f"Итого Marketing: "
            f"<b>{money(total_marketing_spend)}</b>"
        ),
        (
            f"Чистая прибыль после рекламы, бонусов и доставки возвратов: "
            f"<b>{money(actual['profit'] - total_marketing_spend - returned_shipping_loss)}</b>"
        ),
        "",
        "<b>ОТМЕНЫ</b>",
        f"Отменено: <b>{cancelled['orders']}</b> "
        f"на {money(cancelled['revenue'])}",
    ]

    if cancelled["orders"]:
        label = (
            "Потерянная потенциальная прибыль (известная часть)"
            if cancelled["missing_orders"]
            else "Потерянная потенциальная прибыль"
        )
        lines.append(
            f"{label}: <b>{money(cancelled['profit'])}</b>"
        )

    if cancelling["orders"]:
        lines.append(
            f"⏳ В процессе отмены: <b>{cancelling['orders']}</b> "
            f"на {money(cancelling['revenue'])}"
        )

    lines += [
        "",
        "<b>ВОЗВРАТЫ ПО ВЫДАЧАМ ПЕРИОДА</b>",
        f"Возвращено: <b>{returned['orders']}</b> "
        f"на {money(returned['revenue'])}",
    ]

    if returned["orders"]:
        lines.append(
            f"Не возвращается доставка продавца: <b>{money(returned_shipping_loss)}</b>"
            + (f" (нет данных: {returned_shipping_unknown} заказов)" if returned_shipping_unknown else "")
        )
        lines.append(
            "Товары, вернувшиеся на склад, и возвращённая комиссия не считаются потерей. "
            "Реклама уже вычтена выше в общем Marketing."
        )

    if return_requested["orders"]:
        lines.append(
            f"⚠️ Ожидают возврата: <b>{return_requested['orders']}</b> "
            f"на {money(return_requested['revenue'])}"
        )

    missing = set()

    for block in (
        all_created,
        active,
        cancelled,
        cancelling,
        actual,
        returned,
        return_requested,
    ):
        missing.update(block["missing_skus"])

    if missing:
        shown = sorted(missing)[:12]
        suffix = (
            ""
            if len(missing) <= 12
            else f" и ещё {len(missing) - 12}"
        )
        lines += [
            "",
            f"⚠️ <b>Нет себестоимости для {len(missing)} SKU:</b>",
            ", ".join(shown) + suffix,
        ]

    return "\n".join(lines)


def order_delivery_info(order, items):
    seller_known = bool(
        order["delivery_cost_for_seller_known"]
    )

    seller_cost = (
        float(order["delivery_cost_for_seller"] or 0)
        if seller_known
        else None
    )

    order_delivery = float(
        order["delivery_cost"] or 0
    )

    entry_delivery = sum(
        float(item["delivery_cost"] or 0)
        for item in items
    )

    raw_is_kaspi = order["is_kaspi_delivery"]
    is_kaspi = (
        True if raw_is_kaspi == 1
        else False if raw_is_kaspi == 0
        else None
    )

    mode = str(order["delivery_mode"] or "")

    zero_reason = None
    if seller_known and seller_cost == 0:
        if is_kaspi is False and (
            "PICKUP" in mode
            or order["state"] == "PICKUP"
        ):
            zero_reason = (
                "самовывоз: продавцу доставка не начислена"
            )
        elif is_kaspi is False:
            zero_reason = (
                "не Kaspi Доставка; deliveryCost может быть "
                "суммой доставки для покупателя"
            )
        elif is_kaspi is True:
            zero_reason = (
                "Kaspi API действительно передал продавцу 0 ₸"
            )
        else:
            zero_reason = (
                "Kaspi API действительно передал продавцу 0 ₸"
            )

    return {
        "seller_known": seller_known,
        "seller_cost": seller_cost,
        "order_delivery": order_delivery,
        "entry_delivery": entry_delivery,
        "is_kaspi_delivery": is_kaspi,
        "delivery_mode": mode or None,
        "zero_reason": zero_reason,
    }


def item_profit_breakdown(shop_id, order, items):
    """
    Per-item net result.
    Order-level deliveryCostForSeller is allocated proportionally by item revenue.
    """
    total_item_revenue = sum(
        max(0.0, float(item["total_price"] or 0))
        for item in items
    )
    delivery_info = order_delivery_info(
        order,
        items,
    )
    order_delivery = (
        max(
            0.0,
            float(delivery_info["seller_cost"] or 0),
        )
        if delivery_info["seller_known"]
        else 0.0
    )

    result = []

    for item in items:
        revenue = float(item["total_price"] or 0)
        qty = item_quantity(item)
        unit_sale_price = item_unit_price(item)

        if total_item_revenue > 0:
            delivery_share = (
                order_delivery
                * max(0.0, revenue)
                / total_item_revenue
            )
        elif items:
            delivery_share = (
                order_delivery / len(items)
            )
        else:
            delivery_share = 0.0

        cost = DB.cost_for_item(shop_id, item)

        if not cost or qty <= 0:
            result.append({
                "item": item,
                "revenue": revenue,
                "qty": qty,
                "unit_sale_price": unit_sale_price,
                "delivery": (
                    delivery_share
                    if delivery_info["seller_known"]
                    else None
                ),
                "delivery_known": delivery_info["seller_known"],
                "cost_missing": True,
                "cost_unit": None,
                "cogs": None,
                "commission": None,
                "tax": None,
                "acquiring": None,
                "commission_rate": None,
                "tax_rate": None,
                "acquiring_rate": None,
                "packaging": None,
                "other": None,
                "profit": None,
                "margin": None,
            })
            continue

        cost_unit = float(
            cost["cost_price"] or 0
        )
        cogs = cost_unit * qty
        rates = effective_financial_rates(
            shop_id,
            item,
            cost,
        )
        commission = (
            revenue
            * rates["commission_rate"]
            / 100.0
        )
        tax = (
            revenue
            * rates["tax_rate"]
            / 100.0
        )
        acquiring = (
            revenue
            * rates["acquiring_rate"]
            / 100.0
        )
        packaging = (
            float(cost["packaging_cost"] or 0)
            * qty
        )
        other = (
            float(cost["other_cost"] or 0)
            * qty
        )

        profit = (
            revenue
            - cogs
            - commission
            - tax
            - acquiring
            - packaging
            - other
            - delivery_share
        )

        margin = (
            profit / revenue * 100.0
            if revenue
            else 0.0
        )

        result.append({
            "item": item,
            "revenue": revenue,
            "qty": qty,
            "unit_sale_price": unit_sale_price,
            "delivery": (
                delivery_share
                if delivery_info["seller_known"]
                else None
            ),
            "delivery_known": delivery_info["seller_known"],
            "cost_missing": False,
            "cost_unit": cost_unit,
            "cogs": cogs,
            "commission": commission,
            "tax": tax,
            "acquiring": acquiring,
            "commission_rate": rates["commission_rate"],
            "tax_rate": rates["tax_rate"],
            "acquiring_rate": rates["acquiring_rate"],
            "packaging": packaging,
            "other": other,
            "profit": profit,
            "margin": margin,
        })

    return result


def day_orders_for_detail(shop_id, selected):
    """
    One operating day (11:00 -> 11:00):
    include orders created in the period and orders completed/issued
    in the period. Dedupe by order id and preserve both event labels.
    """
    start_ms, end_ms = date_bounds_ms(
        selected,
        selected,
    )

    created = list(
        DB.created_between(
            shop_id,
            start_ms,
            end_ms,
        )
    )
    completed = list(
        DB.completed_between(
            shop_id,
            start_ms,
            end_ms,
        )
    )

    by_id = {}
    labels = {}

    for order in created:
        by_id[order["id"]] = order
        labels.setdefault(
            order["id"],
            [],
        ).append("заказан")

    for order in completed:
        by_id[order["id"]] = order
        labels.setdefault(
            order["id"],
            [],
        ).append("выдан")

    orders = sorted(
        by_id.values(),
        key=lambda order: (
            order["completion_ms"]
            or order["creation_ms"]
            or 0
        ),
    )

    return orders, labels


def _marketing_match_product(row, item):
    row_sku = str(
        row["sku"] or ""
    ).strip().lower()
    item_sku = str(
        item["sku"] or ""
    ).strip().lower()

    row_code = str(
        row["product_code"] or ""
    ).strip().lower()
    item_code = str(
        item["kaspi_product_code"] or ""
    ).strip().lower()

    if row_sku and item_sku and row_sku == item_sku:
        return True

    if row_code and item_code and row_code == item_code:
        return True

    row_name = _marketing_norm_header(
        row["product_name"]
    )
    item_name = _marketing_norm_header(
        item["name"]
    )

    if row_name and item_name and row_name == item_name:
        return True

    return False


def marketing_item_allocations_for_day(
    shop_id,
    selected,
    orders,
    labels=None,
):
    """
    Attach product-level Marketing spend to concrete order items.

    If a Marketing export contains an exact order number, use it.
    Otherwise allocate the product's daily spend proportionally across matching
    sold order items for that day. This avoids repeating the full product spend
    on every order while still showing advertising cost beside each SKU/order.
    """
    marketing_rows = DB.marketing_rows_for_date(
        shop_id,
        selected.isoformat(),
    )

    entries = []

    for order in orders:
        for item in DB.items(
            shop_id,
            order["id"],
        ):
            entries.append({
                "order": order,
                "item": item,
                "event_labels": set(
                    (labels or {}).get(
                        order["id"],
                        [],
                    )
                ),
                "revenue": max(
                    0.0,
                    float(
                        item["total_price"]
                        or 0
                    ),
                ),
            })

    allocations = {}
    allocated_total = 0.0

    for row in marketing_rows:
        amount = float(
            row["amount"]
            or 0
        )

        if abs(amount) < 0.000001:
            continue

        exact_order = str(
            row["order_code"]
            or ""
        ).strip()

        section_key = str(
            row["section_key"]
            or ""
        ).strip()

        if section_key in MARKETING_ACQUISITION_SECTIONS:
            eligible_entries = [
                entry
                for entry in entries
                if "заказан" in entry["event_labels"]
            ]
        elif section_key in MARKETING_ISSUED_BONUS_SECTIONS:
            eligible_entries = [
                entry
                for entry in entries
                if "выдан" in entry["event_labels"]
            ]
        else:
            eligible_entries = list(
                entries
            )

        candidates = []

        if exact_order:
            candidates = [
                entry
                for entry in eligible_entries
                if str(
                    entry["order"]["code"]
                    or entry["order"]["id"]
                ).strip() == exact_order
            ]

            product_candidates = [
                entry
                for entry in candidates
                if _marketing_match_product(
                    row,
                    entry["item"],
                )
            ]
            if product_candidates:
                candidates = product_candidates

        if not candidates and (
            row["sku"]
            or row["product_code"]
            or row["product_name"]
        ):
            candidates = [
                entry
                for entry in eligible_entries
                if _marketing_match_product(
                    row,
                    entry["item"],
                )
            ]

        mapping_quality = str(
            row["mapping_quality"]
            or ""
        )

        if (
            not candidates
            and (
                section_key in MARKETING_ISSUED_BONUS_SECTIONS
                or (
                    section_key in MARKETING_ACQUISITION_SECTIONS
                    and mapping_quality == "section_total_fallback"
                )
            )
        ):
            candidates = list(
                eligible_entries
            )

        if not candidates:
            continue

        weight_total = sum(
            entry["revenue"]
            for entry in candidates
        )

        for entry in candidates:
            if weight_total > 0:
                share = (
                    amount
                    * entry["revenue"]
                    / weight_total
                )
            else:
                share = (
                    amount
                    / len(candidates)
                )

            key = str(
                entry["item"]["entry_id"]
            )

            bucket = allocations.setdefault(
                key,
                {
                    "amount": 0.0,
                    "labels": set(),
                    "sections": {
                        section_key: 0.0
                        for section_key in MARKETING_BREAKDOWN_ORDER
                    },
                },
            )

            bucket["amount"] += share

            section_key = str(
                row["section_key"]
                or ""
            ).strip()

            if section_key in bucket["sections"]:
                bucket["sections"][section_key] += share

            section = MARKETING_REPORT_SECTIONS.get(
                section_key,
                str(
                    row["section_name"]
                    or "Marketing"
                ),
            )
            campaign = str(
                row["campaign_name"]
                or "кампания"
            )

            bucket["labels"].add(
                f"{section}: {campaign}"
            )
            allocated_total += share

    total = sum(
        float(row["amount"] or 0)
        for row in marketing_rows
    )

    return (
        allocations,
        total,
        max(
            0.0,
            total - allocated_total,
        ),
    )


def render_day_detail_compact(shop, selected):
    orders, labels = day_orders_for_detail(
        shop["id"],
        selected,
    )

    title = business_period_label(
        selected,
        selected,
    )

    if not orders:
        return (
            f"📋 Подробный отчёт\n{title}\n\n"
            "Заказов в локальной базе нет."
        )

    marketing_allocations, _, marketing_unallocated = (
        marketing_item_allocations_for_day(
            shop["id"],
            selected,
            orders,
            labels=labels,
        )
    )

    marketing_day_totals = (
        marketing_section_totals_for_period(
            shop["id"],
            selected,
            selected,
        )
    )

    acquisition_spend = sum(
        float(marketing_day_totals.get(key, 0) or 0)
        for key in MARKETING_ACQUISITION_SECTIONS
    )

    issued_bonus_spend = sum(
        float(marketing_day_totals.get(key, 0) or 0)
        for key in MARKETING_ISSUED_BONUS_SECTIONS
    )

    created_count = sum(
        1
        for event_labels in labels.values()
        if "заказан" in event_labels
    )
    issued_count = sum(
        1
        for event_labels in labels.values()
        if "выдан" in event_labels
    )

    lines = [
        f"📋 ПОДРОБНЫЙ ОТЧЁТ",
        title,
        "",
        f"🛒 Заказано: {created_count}",
        (
            f"  Внешняя реклама 11→11: "
            f"{money(marketing_day_totals.get('external', 0))}"
        ),
        (
            f"  Реклама товаров 11→11: "
            f"{money(marketing_day_totals.get('goods', 0))}"
        ),
        f"  Рекламный бюджет: {money(acquisition_spend)}",
        "",
        f"✅ Фактически выдано: {issued_count}",
        (
            f"  Бонус от продавца: "
            f"{money(marketing_day_totals.get('bonus_purchase', 0))}"
        ),
        (
            f"  Бонус за отзыв: "
            f"{money(marketing_day_totals.get('bonus_review', 0))}"
        ),
        f"  Бонусы по факту выдачи: {money(issued_bonus_spend)}",
        "",
        f"Всего заказов в детализации: {len(orders)}",
        "",
    ]

    totals = {
        "revenue": 0.0,
        "cogs": 0.0,
        "commission": 0.0,
        "tax": 0.0,
        "acquiring": 0.0,
        "delivery": 0.0,
        "packaging": 0.0,
        "other": 0.0,
        "profit": 0.0,
    }
    any_missing = False

    for order in orders:
        items = list(
            DB.items(shop["id"], order["id"])
        )
        rows = item_profit_breakdown(
            shop["id"],
            order,
            items,
        )

        order_revenue = float(
            order["total_price"] or 0
        )
        delivery_info = order_delivery_info(
            order,
            items,
        )
        seller_delivery = (
            delivery_info["seller_cost"]
            if delivery_info["seller_known"]
            else None
        )

        totals["revenue"] += order_revenue
        if seller_delivery is not None:
            totals["delivery"] += seller_delivery

        delivery_short = (
            money(seller_delivery)
            if seller_delivery is not None
            else "❓ API не передал"
        )

        delivery_kind = (
            "Kaspi"
            if delivery_info["is_kaspi_delivery"] is True
            else (
                "не Kaspi"
                if delivery_info["is_kaspi_delivery"] is False
                else "тип ?"
            )
        )

        lines.append(
            f"📦 #{order['code'] or order['id']} "
            f"| {money(order_revenue)} "
            f"| 🚚 продавцу {delivery_short} "
            f"[{delivery_kind}]"
        )

        if (
            delivery_info["order_delivery"] > 0
            and (
                seller_delivery is None
                or abs(
                    delivery_info["order_delivery"]
                    - seller_delivery
                ) > 0.001
            )
        ):
            lines.append(
                f"  Доставка в заказе: "
                f"{money(delivery_info['order_delivery'])} "
                f"(не вычитаю как расход продавца)"
            )

        if delivery_info["zero_reason"]:
            lines.append(
                f"  ℹ️ {delivery_info['zero_reason']}"
            )

        if not delivery_info["seller_known"]:
            lines.append(
                "  ⚠️ deliveryCostForSeller отсутствует в API; "
                "это неизвестное значение, а не подтверждённые 0 ₸"
            )

        order_profit_sum = 0.0
        order_complete = True
        order_marketing_total = 0.0
        order_marketing_sections = {
            section_key: 0.0
            for section_key in MARKETING_BREAKDOWN_ORDER
        }

        for row in rows:
            item = row["item"]
            name = str(
                item["name"] or item["sku"] or "Товар"
            ).strip()
            if len(name) > 42:
                name = name[:39] + "…"

            ad_info = marketing_allocations.get(
                str(item["entry_id"]),
                {
                    "amount": 0.0,
                    "labels": set(),
                    "sections": {
                        section_key: 0.0
                        for section_key in MARKETING_BREAKDOWN_ORDER
                    },
                },
            )
            ad_amount = float(
                ad_info["amount"]
                or 0
            )

            order_marketing_total += ad_amount

            for marketing_key in MARKETING_BREAKDOWN_ORDER:
                order_marketing_sections[
                    marketing_key
                ] += float(
                    ad_info["sections"].get(
                        marketing_key,
                        0,
                    )
                    or 0
                )

            if row["cost_missing"]:
                any_missing = True
                order_complete = False
                lines += [
                    f"• {name} ×{row['qty']:g}",
                    f"  Себ: ❓ | прибыль: ❓",
                ]

                if ad_amount:
                    lines.append(
                        "  📣 Marketing:"
                    )
                    lines.extend(
                        marketing_breakdown_plain_lines(
                            ad_info["sections"],
                            indent="    • ",
                        )
                    )
                    lines.append(
                        f"    • Итого Marketing: {money(ad_amount)}"
                    )
                continue

            totals["cogs"] += row["cogs"]
            totals["commission"] += row["commission"]
            totals["tax"] += row["tax"]
            totals["acquiring"] += row["acquiring"]
            totals["packaging"] += row["packaging"]
            totals["other"] += row["other"]
            totals["profit"] += row["profit"]
            order_profit_sum += row["profit"]

            lines += [
                f"• {name} ×{row['qty']:g}",
                (
                    f"  Себ {money(row['cogs'])} | "
                    f"Kaspi {money(row['commission'])} "
                    f"({row['commission_rate']:.2f}%)"
                ),
                (
                    f"  Налог {money(row['tax'])} "
                    f"({row['tax_rate']:.2f}%) | "
                    f"экв. {money(row['acquiring'])} "
                    f"({row['acquiring_rate']:.2f}%)"
                ),
                (
                    f"  Дост. "
                    f"{money(row['delivery']) if row['delivery'] is not None else '❓'} | "
                    f"чистая {money(row['profit'])} "
                    f"({row['margin']:.1f}%)"
                ),
            ]

            if ad_amount:
                lines.append(
                    "  📣 Marketing:"
                )
                lines.extend(
                    marketing_breakdown_plain_lines(
                        ad_info["sections"],
                        indent="    • ",
                    )
                )
                lines.append(
                    f"    • Итого Marketing: {money(ad_amount)}"
                )
                lines.append(
                    f"  После Marketing: "
                    f"{money(row['profit'] - ad_amount)}"
                )

        if order_marketing_total:
            lines.append(
                "  📣 Marketing по заказу:"
            )
            lines.extend(
                marketing_breakdown_plain_lines(
                    order_marketing_sections,
                    indent="    • ",
                )
            )
            lines.append(
                f"    • Итого Marketing: "
                f"{money(order_marketing_total)}"
            )

        if order_complete and order_revenue:
            margin = (
                order_profit_sum
                / order_revenue
                * 100.0
            )
            order_profit_after_marketing = (
                order_profit_sum
                - order_marketing_total
            )
            margin_after_marketing = (
                order_profit_after_marketing
                / order_revenue
                * 100.0
            )

            lines.append(
                f"  ✅ Итого заказ до Marketing: "
                f"{money(order_profit_sum)} "
                f"({margin:.1f}%)"
            )
            lines.append(
                f"  💰 Итого заказ после Marketing: "
                f"{money(order_profit_after_marketing)} "
                f"({margin_after_marketing:.1f}%)"
            )

        lines.append("")

    marketing_rows = DB.marketing_rows_for_date(
        shop["id"],
        selected.isoformat(),
    )
    marketing_spend = sum(
        float(row["amount"] or 0)
        for row in marketing_rows
    )

    profit_after_marketing = (
        totals["profit"]
        - marketing_spend
    )

    total_margin = (
        totals["profit"] / totals["revenue"] * 100.0
        if totals["revenue"]
        else 0.0
    )

    margin_after_marketing = (
        profit_after_marketing
        / totals["revenue"]
        * 100.0
        if totals["revenue"]
        else 0.0
    )

    lines += [
        "━━━━━━━━━━━━━━",
        "ИТОГО",
        f"Выручка: {money(totals['revenue'])}",
        f"Себестоимость: {money(totals['cogs'])}",
        f"Комиссия Kaspi: {money(totals['commission'])}",
        f"Налог: {money(totals['tax'])}",
        f"Эквайринг: {money(totals['acquiring'])}",
        f"Доставка продавца: {money(totals['delivery'])}",
        f"Упаковка: {money(totals['packaging'])}",
        f"Прочие: {money(totals['other'])}",
        f"💰 Прибыль до Marketing: {money(totals['profit'])}",
        f"📈 Маржа до Marketing: {total_margin:.1f}%",
        "📣 MARKETING",
        *marketing_breakdown_plain_lines(
            marketing_section_totals_for_period(
                shop["id"],
                selected,
                selected,
            ),
            indent="  • ",
        ),
        f"  • Итого Marketing: {money(marketing_spend)}",
        (
            f"   └ не распределено по заказам/товарам: "
            f"{money(marketing_unallocated)}"
            if marketing_unallocated > 0.01
            else "   └ по товарам/заказам распределено"
        ),
        f"💰 Чистая прибыль после Marketing: {money(profit_after_marketing)}",
        f"📈 Маржа после Marketing: {margin_after_marketing:.1f}%",
    ]

    # Do not dump raw Marketing report rows into the customer-facing TXT.
    # Relevant spend is already shown beside matched order items above,
    # and the complete daily Marketing total is shown in the totals block.

    if any_missing:
        lines += [
            "",
            "⚠️ Итог по прибыли неполный: "
            "есть SKU без себестоимости.",
        ]

    return "\n".join(lines)


async def callback_day_detail(update, context):
    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    shop = DB.active_shop(
        update.effective_user.id
    )
    if not shop:
        await query.edit_message_text(
            "Сначала подключите магазин."
        )
        return

    try:
        selected = date.fromisoformat(
            query.data.split(":", 1)[1]
        )
    except Exception:
        await query.answer(
            "Не удалось определить дату.",
            show_alert=True,
        )
        return

    report = render_day_detail_compact(
        shop,
        selected,
    )

    # Telegram text message limit is 4096 characters.
    # Keep the user's "one message" requirement: if the report is too large,
    # send exactly one document message instead of splitting it.
    if len(report) <= 3900:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=report,
        )
        return

    payload = io.BytesIO(
        report.encode("utf-8")
    )
    payload.name = (
        f"podrobno_{selected:%Y-%m-%d}.txt"
    )

    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=payload,
        filename=payload.name,
        caption=(
            f"📋 Полный отчёт "
            f"{business_period_label(selected, selected)}\n"
            "Отправлен одним сообщением, потому что "
            "текст превышает лимит Telegram."
        ),
    )


def parse_one_date(text):

    text = text.strip().lower()
    today = current_business_date()

    if text in {"сегодня", "today"}:
        return today

    if text in {"вчера", "yesterday"}:
        return today - timedelta(days=1)

    match = re.search(
        r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b",
        text,
    )

    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3) or today.year)

        if year < 100:
            year += 2000

        return date(year, month, day)

    match = re.search(
        r"\b(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?\b",
        text,
    )

    if match and match.group(2) in MONTHS:
        return date(
            int(match.group(3) or today.year),
            MONTHS[match.group(2)],
            int(match.group(1)),
        )

    return None


def parse_period_text(text):
    low = text.lower().strip()
    today = current_business_date()

    if "сегодня" in low:
        return today, today

    if "вчера" in low:
        d = today - timedelta(days=1)
        return d, d

    match = re.search(
        r"с\s+(\d{1,2})\s+по\s+(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?",
        low,
    )

    if match and match.group(3) in MONTHS:
        month = MONTHS[match.group(3)]
        year = int(match.group(4) or today.year)

        a = date(
            year,
            month,
            int(match.group(1)),
        )
        b = date(
            year,
            month,
            int(match.group(2)),
        )

        return min(a, b), max(a, b)

    parts = re.findall(
        r"\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?",
        low,
    )

    if len(parts) >= 2:
        a = parse_one_date(parts[0])
        b = parse_one_date(parts[1])

        if a and b:
            return min(a, b), max(a, b)

    one = parse_one_date(
        low.replace("за ", "").strip()
    )

    if one:
        return one, one

    return None


KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📊 Отчет за сегодня", "📈 Отчет за неделю"],
        ["📆 Дата", "🗓 Период"],
        ["↩️ Возвраты", "💰 Себестоимость"],
        ["⚙️ Настройки"],
    ],
    resize_keyboard=True,
)


COST_INPUT_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["⬅️ Назад"],
    ],
    resize_keyboard=True,
)


RU_MONTHS = [
    "",
    "Январь", "Февраль", "Март", "Апрель",
    "Май", "Июнь", "Июль", "Август",
    "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def calendar_markup(year, month, mode="single"):
    year = int(year)
    month = int(month)

    prev_month = month - 1
    prev_year = year
    if prev_month < 1:
        prev_month = 12
        prev_year -= 1

    next_month = month + 1
    next_year = year
    if next_month > 12:
        next_month = 1
        next_year += 1

    rows = [[
        InlineKeyboardButton(
            "◀️",
            callback_data=f"calnav:{mode}:{prev_year:04d}-{prev_month:02d}",
        ),
        InlineKeyboardButton(
            f"{RU_MONTHS[month]} {year}",
            callback_data="calnoop",
        ),
        InlineKeyboardButton(
            "▶️",
            callback_data=f"calnav:{mode}:{next_year:04d}-{next_month:02d}",
        ),
    ]]

    rows.append([
        InlineKeyboardButton(day, callback_data="calnoop")
        for day in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
    ])

    for week in calendar.monthcalendar(year, month):
        line = []
        for day in week:
            if day == 0:
                line.append(
                    InlineKeyboardButton(" ", callback_data="calnoop")
                )
            else:
                date_text = f"{year:04d}-{month:02d}-{day:02d}"
                callback_prefix = (
                    "datepick"
                    if mode == "single"
                    else (
                        "periodstart"
                        if mode == "period_start"
                        else "periodend"
                    )
                )
                line.append(
                    InlineKeyboardButton(
                        str(day),
                        callback_data=f"{callback_prefix}:{date_text}",
                    )
                )
        rows.append(line)

    rows.append([
        InlineKeyboardButton(
            "Сегодня",
            callback_data=(
                f"datepick:{current_business_date().isoformat()}"
                if mode == "single"
                else (
                    f"periodstart:{current_business_date().isoformat()}"
                    if mode == "period_start"
                    else f"periodend:{current_business_date().isoformat()}"
                )
            ),
        ),
        InlineKeyboardButton("❌ Закрыть", callback_data="calclose"),
    ])

    return InlineKeyboardMarkup(rows)


async def show_calendar(update, context, mode="single", edit=False, year=None, month=None):
    today = current_business_date()
    year = int(year or today.year)
    month = int(month or today.month)

    if mode == "single":
        title = "📆 <b>Выберите дату отчёта</b>"
    elif mode == "period_start":
        title = "🗓 <b>Выберите начало периода</b>"
    else:
        start = context.user_data.get("period_start")
        start_text = (
            date.fromisoformat(start).strftime("%d.%m.%Y")
            if start
            else "—"
        )
        title = (
            "🗓 <b>Выберите конец периода</b>\n"
            f"Начало: <b>{start_text}</b>"
        )

    markup = calendar_markup(year, month, mode)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            title,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    else:
        await update.effective_message.reply_text(
            title,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


def date_report_markup(start_date, end_date):
    if start_date != end_date:
        return None

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📋 Подробно",
                callback_data=f"daydetail:{start_date.isoformat()}",
            )
        ],
        [
            InlineKeyboardButton(
                "✍️ Себестоимость за этот день",
                callback_data=f"costdate:{start_date.isoformat()}",
            )
        ],
    ])


async def _date_report_worker(application, chat_id, status_message_id, shop_id, user_id, start_date, end_date):
    shop = DB.shop(shop_id, user_id)
    if not shop:
        return

    refresh_note = ""

    # Do not start a second full sync. If full sync is already running,
    # it processes newest dates first in v4.3.
    if shop_id not in RUNNING_SYNCS:
        try:
            await sync_shop_date_range(shop, start_date, end_date)
        except Exception as exc:
            refresh_note = (
                "\n\n⚠️ Не удалось обновить Kaspi прямо сейчас; "
                "показываю данные из локальной базы."
            )
            print("Date quick sync failed:", repr(exc))
    else:
        refresh_note = (
            "\n\n⏳ Полная синхронизация идёт в фоне. "
            "В v4.3 свежие даты загружаются первыми."
        )

    # Refresh Marketing for exactly the requested dates.
    # This fixes missing historical days such as Sep 2 and makes range totals
    # use every day in the selected period instead of only today/yesterday.
    marketing_note = ""

    account = DB.marketing_account(
        shop_id
    )

    if (
        account
        and account["selected_cabinet_name"]
    ):
        if shop_id not in RUNNING_MARKETING_SYNCS:
            RUNNING_MARKETING_SYNCS.add(
                shop_id
            )
            try:
                marketing_result = await marketing_sync_exact_period(
                    shop_id,
                    start_date,
                    end_date,
                )

                if marketing_result["errors"]:
                    marketing_note = (
                        "\n\n⚠️ Marketing обновлён частично; "
                        "показываю всё, что удалось получить."
                    )
            except Exception as exc:
                marketing_note = (
                    "\n\n⚠️ Marketing сейчас не обновился; "
                    "показываю сохранённые расходы."
                )
                print(
                    "Date Marketing sync failed:",
                    repr(exc),
                )
            finally:
                RUNNING_MARKETING_SYNCS.discard(
                    shop_id
                )
        else:
            marketing_note = (
                "\n\n⏳ Marketing уже обновляется в фоне; "
                "показываю текущие сохранённые расходы."
            )

    report = (
        render_report(
            shop,
            start_date,
            end_date,
        )
        + refresh_note
        + marketing_note
    )

    try:
        await application.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_message_id,
            text=report,
            parse_mode=ParseMode.HTML,
            reply_markup=date_report_markup(
                start_date,
                end_date,
            ),
        )
    except Exception as exc:
        print("Could not deliver date report:", repr(exc))


async def send_date_report(update, context, start_date, end_date):
    shop = await require_shop(update)
    if not shop:
        return

    if update.callback_query:
        message = await update.callback_query.edit_message_text(
            "🔄 Обновляю заказы и Marketing за выбранный период…"
        )
    else:
        message = await update.effective_message.reply_text(
            "🔄 Обновляю заказы и Marketing за выбранный период…"
        )

    context.application.create_task(
        _date_report_worker(
            context.application,
            update.effective_chat.id,
            message.message_id,
            shop["id"],
            update.effective_user.id,
            start_date,
            end_date,
        ),
        update=update,
        name=f"date-report-{shop['id']}-{start_date}-{end_date}",
    )


async def callback_calendar(update, context):
    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    data = query.data

    if data == "calnoop":
        return

    if data == "calclose":
        await query.edit_message_text("Календарь закрыт.")
        return

    if data.startswith("calnav:"):
        _, mode, ym = data.split(":", 2)
        year, month = map(int, ym.split("-"))
        await show_calendar(
            update,
            context,
            mode=mode,
            edit=True,
            year=year,
            month=month,
        )
        return

    if data.startswith("datepick:"):
        selected = date.fromisoformat(data.split(":", 1)[1])
        await send_date_report(update, context, selected, selected)
        return

    if data.startswith("periodstart:"):
        selected = date.fromisoformat(data.split(":", 1)[1])
        context.user_data["period_start"] = selected.isoformat()
        await show_calendar(
            update,
            context,
            mode="period_end",
            edit=True,
            year=selected.year,
            month=selected.month,
        )
        return

    if data.startswith("periodend:"):
        selected = date.fromisoformat(data.split(":", 1)[1])
        start_raw = context.user_data.pop("period_start", None)

        if not start_raw:
            await show_calendar(
                update,
                context,
                mode="period_start",
                edit=True,
            )
            return

        start = date.fromisoformat(start_raw)
        a, b = min(start, selected), max(start, selected)
        await send_date_report(update, context, a, b)
        return


def allowed(update):
    if PUBLIC_ACCESS:
        return True

    if not ALLOWED_TELEGRAM_USER_ID:
        return True

    return (
        str(update.effective_user.id)
        == ALLOWED_TELEGRAM_USER_ID
    )


async def guard(update):
    if allowed(update):
        return True

    await update.effective_message.reply_text(
        f"Доступ запрещён. Ваш Telegram User ID: "
        f"{update.effective_user.id}"
    )
    return False


def shops_keyboard(user_id):
    shops = DB.shops_for_user(user_id)
    rows = []

    for shop in shops:
        prefix = "✅ " if shop["is_active"] else ""
        rows.append(
            [
                InlineKeyboardButton(
                    prefix + shop["name"],
                    callback_data=f"shopselect:{shop['id']}",
                ),
                InlineKeyboardButton(
                    "⚙️",
                    callback_data=f"shopmenu:{shop['id']}",
                ),
            ]
        )

    if len(shops) < MAX_SHOPS_PER_USER:
        rows.append(
            [
                InlineKeyboardButton(
                    "➕ Добавить магазин",
                    callback_data="shopadd",
                )
            ]
        )

    return InlineKeyboardMarkup(rows)


def shop_menu_keyboard(shop_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Сделать активным",
                    callback_data=f"shopselect:{shop_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔑 Заменить токен",
                    callback_data=f"shoptoken:{shop_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "✏️ Переименовать",
                    callback_data=f"shoprename:{shop_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑 Удалить",
                    callback_data=f"shopdelete:{shop_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data="shopsback",
                )
            ],
        ]
    )


async def show_shops(update, context, edit=False):
    user_id = update.effective_user.id
    shops = DB.shops_for_user(user_id)

    if shops:
        text = (
            "🏪 <b>Магазины Kaspi</b>\n\n"
            "✅ — активный магазин.\n"
            "Нажмите название, чтобы переключиться.\n\n"
            f"Подключено: <b>{len(shops)}/{MAX_SHOPS_PER_USER}</b>"
        )
    else:
        text = (
            "🏪 <b>Магазины Kaspi</b>\n\n"
            "Пока ни одного магазина не подключено.\n\n"
            f"Можно подключить до <b>{MAX_SHOPS_PER_USER}</b> магазинов."
        )

    markup = shops_keyboard(user_id)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    else:
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


def missing_cost_keyboard(rows):
    buttons = []
    for row in rows:
        label = f"💰 {cost_product_type(row.get('name'), row.get('category'))}: {str(row.get('name') or row['sku'])[:26]}"
        if len(label) > 45:
            label = label[:42] + "…"
        buttons.append([
            InlineKeyboardButton(
                label,
                callback_data=f"costopen:{row['item_rowid']}",
            )
        ])
    return InlineKeyboardMarkup(buttons) if buttons else None


async def send_next_cost_input(update, context, shop):
    """
    Sequential Telegram cost entry:
    automatically picks the next missing SKU and sends its card.
    No SKU-selection screen between products.
    """
    scope_start_ms = context.user_data.get(
        "cost_scope_start_ms"
    )
    scope_end_ms = context.user_data.get(
        "cost_scope_end_ms"
    )
    scope_label = context.user_data.get(
        "cost_scope_label"
    )

    if scope_start_ms is not None and scope_end_ms is not None:
        rows = DB.missing_cost_examples_for_report_period(
            shop["id"],
            scope_start_ms,
            scope_end_ms,
            limit=1,
        )
        remaining = DB.missing_cost_count_for_report_period(
            shop["id"],
            scope_start_ms,
            scope_end_ms,
        )
    else:
        rows = DB.missing_cost_examples(
            shop["id"],
            limit=1,
        )
        remaining = DB.missing_cost_count(
            shop["id"]
        )

    if not rows:
        context.user_data.pop("shop_flow", None)
        context.user_data.pop("cost_shop_id", None)
        context.user_data.pop("cost_rowid", None)
        context.user_data.pop("cost_scope_start_ms", None)
        context.user_data.pop("cost_scope_end_ms", None)
        context.user_data.pop("cost_scope_label", None)

        done_text = (
            f"Себестоимость заполнена для всех SKU отчёта "
            f"<b>{html.escape(scope_label)}</b>."
            if scope_label
            else "Себестоимость заполнена для всех найденных SKU."
        )

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"✅ <b>{html.escape(shop['name'])}</b>\n\n"
                + done_text
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=KEYBOARD,
        )

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="💰 Себестоимость:",
            reply_markup=cost_settings_markup(),
        )
        return False

    rowid = int(rows[0]["item_rowid"])
    item = DB.item_by_rowid(
        shop["id"],
        rowid,
    )

    if not item:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "⚠️ Не удалось открыть следующий товар. "
                "Попробуйте ещё раз открыть настройки себестоимости."
            ),
        )
        return False

    # It may have been filled by another action between the query and now.
    if DB.cost_for_item(shop["id"], item):
        return await send_next_cost_input(
            update,
            context,
            shop,
        )

    context.user_data["shop_flow"] = "cost_input"
    context.user_data["cost_shop_id"] = shop["id"]
    context.user_data["cost_rowid"] = rowid

    position = max(1, remaining)

    scope_line = (
        f"Период: <b>{html.escape(scope_label)}</b>\n"
        if scope_label
        else ""
    )

    caption = (
        f"💰 <b>Введите себестоимость</b>\n\n"
        + scope_line
        + f"Осталось заполнить: <b>{position}</b>\n\n"
        f"Товар: <b>{html.escape(item['name'] or 'Без названия')}</b>\n"
        f"SKU / ID: <code>{html.escape(str(item['sku']))}</code>\n"
        f"Kaspi код: <code>{html.escape(str(item['kaspi_product_code'] or '—'))}</code>\n"
        f"Заказ: <code>{html.escape(str(item['order_code'] or '—'))}</code>\n"
        f"{cost_card_price_lines(item)}\n\n"
        f"Категория: <b>{html.escape(cost_product_type(item['name'], item['category']))}</b>\n"
        "Отправьте себестоимость <b>за 1 штуку</b> в тенге.\n"
        "Например: <code>1600</code>\n\n"
        "Чтобы остановиться без сохранения текущего товара — "
        "нажмите <b>⬅️ Назад</b>."
    )

    image_url = (
        item["image_url"]
        or DB.image_for_sku(
            shop["id"],
            item["sku"],
        )
    )

    image_url = gallery_medium_url(
        image_url
    )

    if not image_url:
        image_url = await resolve_public_product_image(
            item["kaspi_product_code"],
            item["name"],
        )

        if image_url:
            DB.set_image_for_sku(
                shop["id"],
                item["sku"],
                image_url,
            )

    if image_url:
        try:
            preview = await download_product_preview(
                image_url
            )

            if preview:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=preview,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=COST_INPUT_KEYBOARD,
                )
                return True

        except Exception as exc:
            print(
                "Sequential cost preview failed:",
                repr(exc),
            )

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            caption
            + "\n\n📷 Фото для этого товара не найдено."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=COST_INPUT_KEYBOARD,
    )
    return True


async def callback_cost_date(update, context):
    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    shop = DB.active_shop(
        update.effective_user.id
    )
    if not shop:
        await query.edit_message_text(
            "Сначала подключите магазин."
        )
        return

    try:
        selected = date.fromisoformat(
            query.data.split(":", 1)[1]
        )
    except Exception:
        await query.answer(
            "Не удалось определить дату.",
            show_alert=True,
        )
        return

    start_ms, end_ms = date_bounds_ms(
        selected,
        selected,
    )

    context.user_data.pop("shop_flow", None)
    context.user_data.pop("cost_shop_id", None)
    context.user_data.pop("cost_rowid", None)
    context.user_data["cost_scope_start_ms"] = start_ms
    context.user_data["cost_scope_end_ms"] = end_ms
    context.user_data["cost_scope_label"] = selected.strftime(
        "%d.%m.%Y"
    )

    total = DB.missing_cost_count_for_report_period(
        shop["id"],
        start_ms,
        end_ms,
    )

    if not total:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"✅ За <b>{selected:%d.%m.%Y}</b> "
                "у всех SKU из отчёта уже заполнена себестоимость."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=KEYBOARD,
        )
        context.user_data.pop("cost_scope_start_ms", None)
        context.user_data.pop("cost_scope_end_ms", None)
        context.user_data.pop("cost_scope_label", None)
        return

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"✍️ Заполняем только товары отчёта "
            f"<b>{selected:%d.%m.%Y}</b>.\\n"
            f"Без себестоимости: <b>{total}</b>"
        ),
        parse_mode=ParseMode.HTML,
    )

    await send_next_cost_input(
        update,
        context,
        shop,
    )


async def show_missing_costs(update, context, edit=False):
    shop = await require_shop(update)
    if not shop:
        return

    rows = DB.missing_cost_examples(shop["id"], limit=10)
    total = DB.missing_cost_count(shop["id"])

    if not rows:
        text = (
            f"✅ <b>{shop['name']}</b>\n\n"
            "Для всех найденных SKU себестоимость уже заполнена."
        )
        markup = None
    else:
        lines = [
            f"🧾 <b>{shop['name']} — нет себестоимости: {total}</b>",
            "",
            "Нажмите SKU и введите себестоимость за 1 штуку.",
            "Цена сохранится только для этого SKU.",
            "",
        ]
        for i, row in enumerate(rows, 1):
            lines += [
                f"<b>{i}. {row['name']}</b>",
                f"SKU / ID: <code>{row['sku']}</code>",
                f"Пример заказа: <code>{row['order_code'] or '—'}</code>",
                f"Цена за 1 шт. (пример): {money(float(row['sample_price'] or 0))}",
                f"Заказов с этим SKU: {row['order_count']}",
                "",
            ]
        if total > len(rows):
            lines.append(f"Показаны первые {len(rows)} из {total}.")
        text = "\n".join(lines)
        markup = missing_cost_keyboard(rows)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    else:
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


async def callback_costs(update, context):
    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    user_id = update.effective_user.id
    shop = DB.active_shop(user_id)
    if not shop:
        await query.edit_message_text("Сначала подключите магазин: /shops")
        return

    data = query.data
    if data.startswith("costgroup:"):
        choice = data.split(":", 1)[1]
        rowid = context.user_data.get("cost_rowid")
        price = context.user_data.get("proposed_cost")
        item = DB.item_by_rowid(shop["id"], rowid) if rowid else None
        if context.user_data.get("shop_flow") != "cost_group_confirm" or not item or price is None:
            await query.edit_message_text("Предложение устарело. Откройте заполнение ещё раз.")
            return
        if choice == "cancel":
            context.user_data["shop_flow"] = "cost_input"
            context.user_data.pop("proposed_cost", None)
            await query.edit_message_text("Отменено. Отправьте другую сумму или «Назад».")
            return
        if choice not in ("all", "one"):
            return
        DB.set_cost_for_item(shop["id"], item, price, whole_model=(choice == "all"))
        context.user_data.pop("proposed_cost", None)
        context.user_data.pop("cost_rowid", None)
        context.user_data["shop_flow"] = "cost_input"
        await query.edit_message_text(
            f"✅ Сохранено {money(price)} за 1 шт. " +
            ("для всех цветов этой модели" if choice == "all" else "только для этого товара")
        )
        await send_next_cost_input(update, context, shop)
        return
    if data == "missing:list":
        await show_missing_costs(update, context, edit=True)
        return

    if data.startswith("costopen:"):
        rowid = int(data.split(":", 1)[1])
        item = DB.item_by_rowid(shop["id"], rowid)
        if not item:
            await query.edit_message_text(
                "Этот товар уже не найден в базе. Откройте /missing ещё раз."
            )
            return

        # Если стоимость уже появилась между показом списка и нажатием — не просим повторно.
        if DB.cost_for_item(shop["id"], item):
            await query.edit_message_text(
                f"✅ Для <code>{item['sku']}</code> себестоимость уже заполнена.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "⬅️ К списку", callback_data="missing:list"
                )]]),
            )
            return

        context.user_data.clear()
        context.user_data["shop_flow"] = "cost_input"
        context.user_data["cost_shop_id"] = shop["id"]
        context.user_data["cost_rowid"] = rowid

        caption = (
            f"💰 <b>Введите себестоимость SKU</b>\n\n"
            f"Товар: <b>{html.escape(item['name'] or 'Без названия')}</b>\n"
            f"SKU / ID: <code>{html.escape(str(item['sku']))}</code>\n"
            f"Kaspi код: <code>{html.escape(str(item['kaspi_product_code'] or '—'))}</code>\n"
            f"Заказ: <code>{html.escape(str(item['order_code'] or '—'))}</code>\n"
            f"{cost_card_price_lines(item)}\n\n"
            "Себестоимость <b>за 1 шт.</b> сохранится для выбранного товара "
            "или после подтверждения для всех цветов модели.\n"
            "После сохранения бот автоматически покажет следующий "
            "товар без себестоимости.\n\n"
            "Отправьте только число в тенге. Например: <code>1600</code>"
        )

        image_url = (
            item["image_url"]
            or DB.image_for_sku(shop["id"], item["sku"])
        )

        # v4.3/v4.4 could cache a logo or unrelated asset.
        # Reuse cache only when it is a genuine Kaspi product-photo CDN URL.
        image_url = gallery_medium_url(image_url)

        if not image_url:
            image_url = await resolve_public_product_image(
                item["kaspi_product_code"],
                item["name"],
            )

            if image_url:
                DB.set_image_for_sku(
                    shop["id"],
                    item["sku"],
                    image_url,
                )

        # Keep the list message concise and send a separate product card.
        try:
            await query.edit_message_text(
                "👇 Карточка товара для заполнения себестоимости:"
            )
        except Exception:
            pass

        if image_url:
            try:
                preview = await download_product_preview(image_url)

                if preview:
                    await context.bot.send_photo(
                        chat_id=update.effective_chat.id,
                        photo=preview,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                    )
                    return

            except Exception as exc:
                print("Telegram could not send product preview:", repr(exc))

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=caption + (
                "\n\n📷 Точное фото этого Kaspi-кода автоматически "
                "найти не удалось."
            ),
            parse_mode=ParseMode.HTML,
        )


async def cmd_missing(update, context):
    if not await guard(update):
        return
    await show_missing_costs(update, context)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    await update.message.reply_text(
        "Kaspi Profit Bot MVP.\n\n"
        f"Ваш Telegram User ID: "
        f"{update.effective_user.id}\n\n"
        "/shops — подключить магазины\n"
        "/today — сегодня\n"
        "/yesterday — вчера\n"
        "/date 01.09.2026 — конкретная дата\n"
        "/period 01.09.2026 05.09.2026 — период\n"
        "Или используйте кнопки 📆/🗓 календаря.\n"
        "/sync — обновить Kaspi\n"
        "/missing — товары без себестоимости\n"
        "/costs — загрузка себестоимости CSV\n"
        "/status — состояние базы",
        reply_markup=KEYBOARD,
    )


async def cmd_shops(update, context):
    if not await guard(update):
        return
    await show_shops(update, context)


async def callback_shops(update, context):
    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    user_id = update.effective_user.id
    data = query.data

    if data == "shopadd":
        if DB.shop_count_for_user(user_id) >= MAX_SHOPS_PER_USER:
            await query.edit_message_text(
                f"Достигнут лимит: {MAX_SHOPS_PER_USER} магазинов на пользователя.",
                reply_markup=shops_keyboard(user_id),
            )
            return

        context.user_data.clear()
        context.user_data["shop_flow"] = "add_name"

        await query.edit_message_text(
            "➕ <b>Новый магазин</b>\n\n"
            "Напишите удобное название магазина.\n"
            "Например: <b>Ambrello</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "shopsback":
        await show_shops(
            update,
            context,
            edit=True,
        )
        return

    action, raw_id = data.split(":", 1)
    shop_id = int(raw_id)
    shop = DB.shop(shop_id, user_id)

    if not shop:
        await query.edit_message_text(
            "Магазин не найден."
        )
        return

    if action == "shopselect":
        DB.set_active_shop(
            user_id,
            shop_id,
        )
        await show_shops(
            update,
            context,
            edit=True,
        )
        return

    if action == "shopmenu":
        active = "✅ активный" if shop["is_active"] else "неактивный"

        await query.edit_message_text(
            f"🏪 <b>{shop['name']}</b>\n"
            f"Статус: {active}\n\n"
            "Что изменить?",
            parse_mode=ParseMode.HTML,
            reply_markup=shop_menu_keyboard(shop_id),
        )
        return

    if action == "shoptoken":
        context.user_data.clear()
        context.user_data["shop_flow"] = "edit_token"
        context.user_data["shop_id"] = shop_id

        await query.edit_message_text(
            f"🔑 <b>{shop['name']}</b>\n\n"
            "Отправьте новый X-Auth-Token Kaspi "
            "одним сообщением.\n\n"
            "После получения бот попробует удалить "
            "сообщение и сохранит токен зашифрованным.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "shoprename":
        context.user_data.clear()
        context.user_data["shop_flow"] = "rename"
        context.user_data["shop_id"] = shop_id

        await query.edit_message_text(
            f"✏️ Текущее название: <b>{shop['name']}</b>\n\n"
            "Напишите новое название.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "shopdelete":
        await query.edit_message_text(
            f"🗑 Удалить магазин <b>{shop['name']}</b>?\n\n"
            "Будут удалены его сохранённый токен, "
            "заказы и себестоимость из локальной базы.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Да, удалить",
                            callback_data=f"shopdeleteyes:{shop_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Отмена",
                            callback_data=f"shopmenu:{shop_id}",
                        )
                    ],
                ]
            ),
        )
        return

    if action == "shopdeleteyes":
        name = shop["name"]
        DB.delete_shop(
            user_id,
            shop_id,
        )

        await query.edit_message_text(
            f"✅ Магазин <b>{name}</b> удалён.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ К магазинам",
                            callback_data="shopsback",
                        )
                    ]
                ]
            ),
        )


async def require_shop(update):
    shop = DB.active_shop(
        update.effective_user.id
    )

    if not shop:
        await update.effective_message.reply_text(
            "Сначала подключите магазин: /shops"
        )
        return None

    return shop




def returns_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Сегодня", callback_data="returns:today"),
            InlineKeyboardButton("7 дней", callback_data="returns:week"),
            InlineKeyboardButton("30 дней", callback_data="returns:month"),
        ],
        [InlineKeyboardButton("🗓 Свой период", callback_data="returns:custom")],
    ])


def render_returns_report(shop, start_date, end_date):
    orders = list(DB.returns_discovered_between(shop["id"], start_date, end_date))
    result = return_loss_summary(shop["id"], orders)
    lines = [
        f"↩️ <b>Возвраты — {html.escape(shop['name'])}</b>",
        f"Период обнаружения: <b>{business_period_label(start_date, end_date)}</b>",
        "",
        f"Возвращённых заказов: <b>{result['orders']}</b>",
        f"Товаров в них: <b>{quantity_label(result['units'])} шт.</b>",
        f"Сумма возвращённых продаж: <b>{money(result['refunded_revenue'])}</b>",
        f"Товаров подтверждённо вернулось на склад: <b>{quantity_label(result['restocked_units'])} шт.</b>",
        f"Себестоимость возвращённого товара (актив, не расход): <b>{money(result['stock_value_recovered'])}</b>",
        f"Комиссия Kaspi к возврату (оценка по тарифу): <b>{money(result['commission_reversed_estimate'])}</b>",
        "",
        "<b>Безвозвратные расходы</b>",
        f"Доставка продавца: <b>{money(result['delivery'])}</b>",
        f"Реклама, напрямую связанная с кодами возвратных заказов: <b>{money(result['marketing_attributed'])}</b>",
        f"Итого подтверждённый минус: <b>−{money(result['confirmed_unrecoverable'])}</b>",
    ]
    if result["delivery_unknown"]:
        lines.append(f"⚠️ Неизвестна стоимость доставки: {result['delivery_unknown']} заказов")
    if result["ads_unattributed_orders"]:
        lines.append(
            f"⚠️ Для {result['ads_unattributed_orders']} заказов нет точной привязки рекламных расходов. "
            "Реальный минус может быть больше."
        )
    if result["unconfirmed_warehouse_orders"]:
        lines.append(
            f"⚠️ Для {result['unconfirmed_warehouse_orders']} возвратов Kaspi не подтвердил получение на склад. "
            "Стоимость этих товаров не добавлена в возвратный товарный остаток."
        )
    if result["missing_cost_units"]:
        lines.append("⚠️ Не у всех SKU заполнена себестоимость; оценка восстановленного запаса неполная.")
    lines += [
        "",
        "ℹ️ Возврат денег покупателю, возврат себестоимости товара в складской актив "
        "и возврат комиссии не считаются безвозвратным расходом.",
        "Показывается дата первого обнаружения статуса RETURNED ботом, "
        "а не гарантированная фактическая дата возврата Kaspi. "
        "Для старых заказов это дата первой синхронизации их возвратного статуса.",
        "Реклама, учтённая в общем отчёте Marketing, не должна вычитаться второй раз.",
        "Данные берутся из последней синхронизации; для обновления статусов заказов воспользуйтесь /sync.",
    ]
    return "\n".join(lines)


async def send_returns_report(update, context, start_date, end_date):
    shop = await require_shop(update)
    if not shop:
        return
    # Return status can change for orders created months before this report's
    # selected dates. Do not mistakenly refresh only orders created in this
    # date window; the 90-day sync (/sync or scheduler) updates these statuses.
    text = render_returns_report(shop, start_date, end_date)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=returns_markup()
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=returns_markup()
        )


async def cmd_returns(update, context):
    if not await guard(update):
        return
    shop = await require_shop(update)
    if not shop:
        return
    await update.effective_message.reply_text(
        "↩️ <b>Возвраты</b>\n"
        "Покажу количество возвратов, товары на складе, доставку и рекламу, "
        "которые не возмещаются. Выберите период:",
        parse_mode=ParseMode.HTML, reply_markup=returns_markup()
    )


async def callback_returns(update, context):
    q = update.callback_query
    await q.answer()
    if not await guard(update):
        return
    action = q.data.split(":", 1)[1]
    today = current_business_date()
    if action == "today":
        return await send_returns_report(update, context, today, today)
    if action == "week":
        return await send_returns_report(update, context, today - timedelta(days=6), today)
    if action == "month":
        return await send_returns_report(update, context, today - timedelta(days=29), today)
    if action == "custom":
        context.user_data["shop_flow"] = "returns_period_input"
        await q.edit_message_text(
            "🗓 Введите две даты через пробел, например:\n"
            "<code>01.10.2026 23.10.2026</code>\n\n"
            "Период считается по дате обнаружения возврата ботом.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_today(update, context):
    if not await guard(update):
        return

    d = current_business_date()
    await send_date_report(update, context, d, d)


async def cmd_yesterday(update, context):
    if not await guard(update):
        return

    d = current_business_date() - timedelta(days=1)
    await send_date_report(update, context, d, d)


async def cmd_week(update, context):
    if not await guard(update):
        return

    today = current_business_date()

    await send_date_report(
        update,
        context,
        today - timedelta(days=6),
        today,
    )


async def cmd_date(update, context):
    if not await guard(update):
        return

    shop = await require_shop(update)
    if not shop:
        return

    d = parse_one_date(
        " ".join(context.args)
    )

    if not d:
        await update.message.reply_text(
            "Формат: /date 01.09.2026"
        )
        return

    await send_date_report(update, context, d, d)


async def cmd_period(update, context):
    if not await guard(update):
        return

    shop = await require_shop(update)
    if not shop:
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Формат: /period 01.09.2026 05.09.2026"
        )
        return

    a = parse_one_date(context.args[0])
    b = parse_one_date(context.args[1])

    if not a or not b:
        await update.message.reply_text(
            "Формат: /period 01.09.2026 05.09.2026"
        )
        return

    a, b = min(a, b), max(a, b)

    await send_date_report(update, context, a, b)


async def _safe_edit_sync_message(application, chat_id, message_id, text, markup=None):
    try:
        await application.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    except (NetworkError, TimedOut):
        # Telegram is temporarily unreachable. The sync result remains
        # in the local DB and will be visible on the next command.
        print(
            "Telegram unavailable while sending sync result "
            f"chat={chat_id}, message={message_id}"
        )
    except Exception as exc:
        print("Could not edit sync message:", repr(exc))


async def _manual_sync_worker(application, chat_id, message_id, shop_id, user_id):
    try:
        shop = DB.shop(shop_id, user_id)
        if not shop:
            await _safe_edit_sync_message(
                application,
                chat_id,
                message_id,
                "❌ Магазин больше не найден.",
            )
            return

        count = await sync_shop(shop)

        missing_count = DB.missing_cost_count(shop["id"])
        result_text = (
            f"✅ <b>{shop['name']}</b>\n"
            f"Получено/обновлено заказов: {count}"
        )

        markup = None
        if missing_count:
            result_text += (
                f"\n\n⚠️ Без себестоимости: "
                f"<b>{missing_count}</b> SKU"
            )
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "💰 Заполнить себестоимость",
                    callback_data="missing:list",
                )
            ]])

        await _safe_edit_sync_message(
            application,
            chat_id,
            message_id,
            result_text,
            markup,
        )

    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        detail = (exc.response.text or "").strip()

        if len(detail) > 700:
            detail = detail[:700] + "…"

        result_text = (
            f"❌ Kaspi API вернул HTTP {code}.\n"
            "Токен мог быть корректным — ошибка также может быть "
            "в параметрах запроса.\n"
        )

        if detail:
            result_text += f"\nОтвет Kaspi:\n{detail}"

        await _safe_edit_sync_message(
            application,
            chat_id,
            message_id,
            result_text,
        )

    except (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.PoolTimeout,
    ) as exc:
        await _safe_edit_sync_message(
            application,
            chat_id,
            message_id,
            "⚠️ Не удалось закончить синхронизацию из-за временной "
            f"проблемы сети: {type(exc).__name__}.\n"
            "Данные, которые успели загрузиться, сохранены. "
            "Повторите синхронизацию позже.",
        )

    except Exception as exc:
        await _safe_edit_sync_message(
            application,
            chat_id,
            message_id,
            f"❌ Ошибка синхронизации: "
            f"{type(exc).__name__}: {exc}",
        )

    finally:
        RUNNING_SYNCS.discard(shop_id)


async def cmd_sync(update, context):
    if not await guard(update):
        return

    shop = await require_shop(update)
    if not shop:
        return

    shop_id = shop["id"]

    if shop_id in RUNNING_SYNCS:
        await update.message.reply_text(
            f"⏳ <b>{shop['name']}</b> уже синхронизируется.\n"
            "Можно пользоваться остальными кнопками — "
            "синхронизация идёт в фоне.",
            parse_mode=ParseMode.HTML,
        )
        return

    message = await update.message.reply_text(
        f"🔄 <b>{shop['name']}</b>: синхронизация запущена в фоне.\n"
        "Ботом можно продолжать пользоваться.",
        parse_mode=ParseMode.HTML,
    )

    RUNNING_SYNCS.add(shop_id)

    context.application.create_task(
        _manual_sync_worker(
            context.application,
            update.effective_chat.id,
            message.message_id,
            shop_id,
            update.effective_user.id,
        ),
        update=update,
        name=f"manual-sync-{shop_id}",
    )


async def cmd_repairgroups(update, context):
    if not await guard(update):
        return

    shop = await require_shop(update)
    if not shop:
        return

    suspicious = DB.unsafe_model_groups(shop["id"])

    if not suspicious:
        await update.effective_message.reply_text(
            "✅ Подозрительных массовых групп себестоимости не найдено."
        )
        return

    rows = []
    lines = [
        f"🛠 <b>Найдены подозрительные группы: {len(suspicious)}</b>",
        "",
    ]

    for index, item in enumerate(suspicious[:10], start=1):
        group = item["group"]
        variants = item["variants"]
        suffix = group["group_key"].split("::", 1)[-1]

        lines += [
            f"{index}. <b>{html.escape(group['display_name'] or suffix)}</b>",
            f"SKU в группе: <b>{len(variants)}</b>",
            f"Цена: <b>{money(float(group['cost_price']))}</b>",
            "",
        ]

        rows.append([
            InlineKeyboardButton(
                f"🗑 Исправить группу #{index} ({len(variants)} SKU)",
                callback_data=f"repairgroup:{group['id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "↩️ Восстановить последнюю очистку",
            callback_data="repairrestore",
        )
    ])

    await update.effective_message.reply_text(
        "\n".join(lines)
        + "Перед удалением строки себестоимости будут сохранены "
          "в резервную таблицу.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def callback_repairgroups(update, context):
    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    shop = DB.active_shop(update.effective_user.id)
    if not shop:
        await query.edit_message_text("Сначала подключите магазин.")
        return

    if query.data == "repairrestore":
        restored = DB.restore_last_repair(shop["id"])
        await query.edit_message_text(
            f"↩️ Восстановлено строк себестоимости: {restored}"
        )
        return

    if query.data.startswith("repairgroup:"):
        group_id = int(query.data.split(":", 1)[1])
        group = DB.conn.execute(
            """SELECT * FROM model_costs
               WHERE id=? AND shop_id=?""",
            (group_id, shop["id"]),
        ).fetchone()

        if not group:
            await query.edit_message_text(
                "Эта группа уже удалена или не найдена."
            )
            return

        variants = DB.matching_legacy_model_variants(
            shop["id"],
            group["group_key"],
        )

        await query.edit_message_text(
            f"⚠️ <b>Подтвердить очистку?</b>\\n\\n"
            f"Группа: <b>{html.escape(group['display_name'] or group['group_key'])}</b>\\n"
            f"Вариантов: <b>{len(variants)}</b>\\n"
            f"Цена группы: <b>{money(float(group['cost_price']))}</b>\\n\\n"
            "Будут удалены только текущие строки себестоимости этой группы, "
            "у которых цена совпадает с ошибочно размноженной ценой. "
            "Перед удалением они попадут в резервную таблицу.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Да, исправить",
                        callback_data=f"repairconfirm:{group_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Отмена",
                        callback_data="repaircancel",
                    )
                ],
            ]),
        )
        return

    if query.data == "repaircancel":
        await query.edit_message_text("Очистка отменена.")
        return

    if query.data.startswith("repairconfirm:"):
        group_id = int(query.data.split(":", 1)[1])
        group = DB.conn.execute(
            """SELECT * FROM model_costs
               WHERE id=? AND shop_id=?""",
            (group_id, shop["id"]),
        ).fetchone()

        if not group:
            await query.edit_message_text(
                "Группа уже удалена или не найдена."
            )
            return

        removed = DB.repair_unsafe_model_group(
            shop["id"],
            group["group_key"],
        )

        await query.edit_message_text(
            f"✅ Ошибочная массовая группа удалена.\\n"
            f"Сброшено строк себестоимости: <b>{removed}</b>\\n\\n"
            "Теперь они снова появятся в «Без себестоимости». "
            "Если нужно отменить очистку — /repairgroups → "
            "«Восстановить последнюю очистку».",
            parse_mode=ParseMode.HTML,
        )


async def cmd_deliverydebug(update, context):
    if not await guard(update):
        return

    shop = await require_shop(update)
    if not shop:
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /deliverydebug НОМЕР_ЗАКАЗА"
        )
        return

    code = context.args[0].strip()
    order = DB.order_by_code(
        shop["id"],
        code,
    )

    if not order:
        await update.message.reply_text(
            "Заказ не найден в локальной базе. "
            "Сначала выполните синхронизацию."
        )
        return

    items = list(
        DB.items(
            shop["id"],
            order["id"],
        )
    )
    info = order_delivery_info(
        order,
        items,
    )

    seller = (
        money(info["seller_cost"])
        if info["seller_known"]
        else "❓ поле отсутствовало"
    )

    lines = [
        f"🚚 <b>Доставка #{html.escape(str(code))}</b>",
        "",
        f"Для продавца: <b>{seller}</b>",
        f"deliveryCost заказа: <b>{money(info['order_delivery'])}</b>",
        f"deliveryCost позиций: <b>{money(info['entry_delivery'])}</b>",
        f"Kaspi Доставка: <b>{html.escape(str(info['is_kaspi_delivery']))}</b>",
        f"deliveryMode: <code>{html.escape(str(info['delivery_mode'] or '—'))}</code>",
        f"state: <code>{html.escape(str(order['state'] or '—'))}</code>",
        f"status: <code>{html.escape(str(order['status'] or '—'))}</code>",
    ]

    if info["zero_reason"]:
        lines += [
            "",
            f"ℹ️ {html.escape(info['zero_reason'])}",
        ]

    if not info["seller_known"]:
        lines += [
            "",
            "⚠️ Раньше отсутствие deliveryCostForSeller "
            "ошибочно выглядело как 0 ₸.",
        ]

    await update.message.reply_text(
        "\\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def cmd_phototest(update, context):
    if not await guard(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Формат: /phototest 123456789"
        )
        return

    code = str(context.args[0]).strip()

    status = await update.effective_message.reply_text(
        f"📷 Ищу товарное фото Kaspi для кода {code}…"
    )

    image_url = await resolve_public_product_image(code)

    if not image_url:
        await status.edit_text(
            f"❌ Для Kaspi-кода {code} точное фото не найдено."
        )
        return

    preview = await download_product_preview(image_url)

    if not preview:
        await status.edit_text(
            "⚠️ Ссылку на товарное фото нашёл, "
            "но скачать изображение не удалось.\n\n"
            f"{image_url}"
        )
        return

    try:
        await status.delete()
    except Exception:
        pass

    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=preview,
        caption=(
            f"✅ Фото найдено\n"
            f"Kaspi-код: <code>{html.escape(code)}</code>\n"
            f"<code>{html.escape(image_url)}</code>"
        ),
        parse_mode=ParseMode.HTML,
    )


async def cmd_apitest(update, context):
    if not await guard(update):
        return

    shop = await require_shop(update)
    if not shop:
        return

    d = current_business_date()
    start_dt, end_dt = business_day_bounds_dt(d, d)

    message = await update.effective_message.reply_text(
        f"🧪 Проверяю Kaspi API за {d:%d.%m.%Y} по всем состояниям…"
    )

    try:
        api = KaspiAPI(shop_token(shop))

        orders, stats = await api.list_orders(
            int(start_dt.timestamp() * 1000),
            int(end_dt.timestamp() * 1000),
            with_stats=True,
        )

        lines = [
            f"🧪 <b>{shop['name']}</b>",
            f"Дата: <b>{d:%d.%m.%Y}</b>",
            f"Уникальных заказов: <b>{len(orders)}</b>",
            "",
        ]

        for state in ORDER_STATES:
            lines.append(
                f"{state}: <b>{stats.get(state, 0)}</b>"
            )

        if not orders:
            lines += [
                "",
                "⚠️ API ответил успешно, но по всем состояниям вернул 0.",
            ]

        await message.edit_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    except Exception as exc:
        await message.edit_text(
            f"❌ API test: {type(exc).__name__}: {exc}"
        )


_MARKETING_PW = None
_MARKETING_BROWSER = None
_MARKETING_LOCK = asyncio.Lock()
ACTIVE_MARKETING = {}


async def marketing_browser_diagnostics():
    """Checks Chromium and the login page without accessing account credentials."""
    try:
        browser = await _marketing_browser()
        context = await browser.new_context(locale="ru-RU")
        try:
            page = await context.new_page()
            response = await page.goto(
                KASPI_MARKETING_SIGN_IN_URL,
                wait_until="domcontentloaded",
                timeout=min(MARKETING_BROWSER_TIMEOUT_MS, 30000),
            )
            status = str(response.status) if response else "нет ответа HTTP"
            return (
                "✅ Chromium запущен.\n"
                f"Браузер: <code>{html.escape(str(browser.version))}</code>\n"
                f"Kaspi Marketing: HTTP {html.escape(status)}\n"
                f"Финальная страница: <code>{html.escape(page.url[:250])}</code>\n"
                "Проверка браузера не означает успешную авторизацию аккаунта."
            )
        finally:
            await context.close()
    except Exception as exc:
        error = str(exc).replace(TELEGRAM_BOT_TOKEN, "[TOKEN]") if TELEGRAM_BOT_TOKEN else str(exc)
        return (
            "❌ Проверка Chromium / Kaspi Marketing не прошла.\n"
            f"Ошибка: <code>{html.escape(type(exc).__name__ + ': ' + error[:500])}</code>\n"
            "Проверьте версию Playwright, доступность Kaspi и повторите."
        )


def marketing_storage_file(shop_id):
    return MARKETING_DIR / f"shop_{shop_id}_storage.bin"


def marketing_screen_file(shop_id):
    return MARKETING_DIR / f"shop_{shop_id}_screen.png"


async def _marketing_browser():
    global _MARKETING_PW, _MARKETING_BROWSER
    if _MARKETING_BROWSER is None:
        _MARKETING_PW = await async_playwright().start()
        _MARKETING_BROWSER = await _MARKETING_PW.chromium.launch(
            headless=MARKETING_HEADLESS,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
    return _MARKETING_BROWSER


def _marketing_load_storage(shop_id):
    path = marketing_storage_file(shop_id)
    if not path.exists():
        return None
    try:
        raw = FERNET.decrypt(path.read_bytes())
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


async def _marketing_context(shop_id):
    browser = await _marketing_browser()
    kwargs = {
        "viewport": {"width": 1440, "height": 1000},
        "locale": "ru-RU",
        "timezone_id": "Asia/Almaty",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
    }
    storage = _marketing_load_storage(shop_id)
    if storage:
        kwargs["storage_state"] = storage
    return await browser.new_context(**kwargs)


async def _marketing_save_storage(shop_id, context):
    raw = json.dumps(
        await context.storage_state(),
        ensure_ascii=False,
    ).encode("utf-8")
    marketing_storage_file(shop_id).write_bytes(
        FERNET.encrypt(raw)
    )


async def _marketing_close(shop_id):
    active = ACTIVE_MARKETING.pop(shop_id, None)
    if active:
        try:
            await active["context"].close()
        except Exception:
            pass


async def _marketing_screen(shop_id, page):
    try:
        data = await page.screenshot(full_page=False, type="png")
        marketing_screen_file(shop_id).write_bytes(data)
    except Exception:
        pass


async def _first_visible(page, selectors):
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            pass
    return None


def _marketing_url_parts(url):
    try:
        parsed = urlparse(
            str(url or "")
        )
        return (
            parsed.hostname or "",
            parsed.path or "/",
        )
    except Exception:
        return "", "/"


def _is_marketing_host(url):
    host, _ = _marketing_url_parts(
        url
    )
    return (
        host == "marketing.kaspi.kz"
        or host.endswith(
            ".marketing.kaspi.kz"
        )
    )


def _is_marketing_signin_url(url):
    host, path = _marketing_url_parts(
        url
    )
    return (
        host == "marketing.kaspi.kz"
        and path.rstrip("/") == "/sign-in"
    )


async def _wait_for_marketing_login_form(page, timeout_ms=12000):
    """
    SPA can initially render no inputs. Wait specifically for the
    password field before deciding that the sign-in page is unusable.
    """
    password = page.locator(
        'input[type="password"], '
        'input[name*="password"], '
        'input[id*="password"]'
    ).first

    try:
        await password.wait_for(
            state="visible",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


async def _marketing_state(page):
    try:
        body = (await page.locator("body").inner_text(timeout=5000)).lower()
    except Exception:
        body = ""

    if "приложение не поддерживается" in body:
        return "unsupported"

    otp = await _first_visible(
        page,
        [
            'input[autocomplete="one-time-code"]',
            'input[name*="otp"]',
            'input[id*="otp"]',
            'input[name*="code"]',
            'input[id*="code"]',
        ],
    )
    if otp or "введите код" in body or "код из sms" in body or "код из смс" in body:
        return "otp"

    if "подтвердите вход" in body or "подтвердите авторизацию" in body:
        return "confirmation"

    password = await _first_visible(
        page,
        ['input[type="password"]', 'input[name*="password"]', 'input[id*="password"]'],
    )
    if password:
        return "login"

    # The /sign-in SPA may momentarily have no password input while loading.
    # Never treat that route as an authenticated dashboard.
    if _is_marketing_signin_url(
        page.url
    ):
        return "login"

    # Connected only when we are still on the real Marketing host
    # and have actually left the sign-in route.
    if _is_marketing_host(
        page.url
    ):
        return "connected"

    return "redirected"


async def _marketing_submit(page):
    button = await _first_visible(
        page,
        [
            'button[type="submit"]',
            'button:has-text("Войти")',
            'button:has-text("Продолжить")',
            'button:has-text("Далее")',
        ],
    )
    if not button:
        return False
    await button.click()
    return True


def normalize_marketing_phone(value):
    digits = re.sub(
        r"\\D+",
        "",
        str(value or ""),
    )

    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = digits[1:]

    if len(digits) != 10:
        return None

    return digits


async def marketing_login_direct(shop_id, login, password):
    async with _MARKETING_LOCK:
        await _marketing_close(shop_id)
        context = await _marketing_context(shop_id)
        page = await context.new_page()
        ACTIVE_MARKETING[shop_id] = {"context": context, "page": page}

        try:
            await page.goto(
                KASPI_MARKETING_SIGN_IN_URL,
                wait_until="domcontentloaded",
                timeout=MARKETING_BROWSER_TIMEOUT_MS,
            )
            await page.wait_for_timeout(1200)

            # Wait for the actual sign-in form. This avoids falsely treating
            # an incompletely-loaded SPA as an authenticated page.
            await _wait_for_marketing_login_form(
                page,
                timeout_ms=12000,
            )
        except Exception as exc:
            await _marketing_screen(shop_id, page)
            return "error", f"Не удалось открыть marketing.kaspi.kz: {exc}"

        state = await _marketing_state(page)
        if state == "connected":
            await _marketing_save_storage(shop_id, context)
            await _marketing_close(shop_id)
            return "connected", None

        if state == "unsupported":
            await _marketing_screen(shop_id, page)
            return "unsupported", "Kaspi Marketing сообщил, что браузер не поддерживается."

        login_input = await _first_visible(
            page,
            [
                'input[type="tel"]',
                'input[autocomplete="username"]',
                'input[name*="phone"]',
                'input[id*="phone"]',
                'input[name*="login"]',
                'input[id*="login"]',
                'input[type="text"]',
            ],
        )
        password_input = await _first_visible(
            page,
            ['input[type="password"]', 'input[name*="password"]', 'input[id*="password"]'],
        )

        if not login_input or not password_input:
            await _marketing_screen(shop_id, page)
            return "error", "Не удалось найти поля логина и пароля на marketing.kaspi.kz."

        phone = (
            normalize_marketing_phone(
                login
            )
            or str(login).strip()
        )

        await login_input.fill(
            phone
        )
        await password_input.fill(str(password))

        if not await _marketing_submit(page):
            await _marketing_screen(shop_id, page)
            return "error", "Не удалось найти кнопку «Войти»."

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

        await page.wait_for_timeout(2500)
        state = await _marketing_state(page)

        # If the form sent us to some unrelated host/page, don't pretend
        # we're authenticated. Re-open the exact Marketing sign-in URL once:
        # if cookies/session are valid, Marketing itself should take us into
        # the authenticated interface.
        if state == "redirected":
            redirected_url = page.url

            try:
                await page.goto(
                    KASPI_MARKETING_SIGN_IN_URL,
                    wait_until="domcontentloaded",
                    timeout=MARKETING_BROWSER_TIMEOUT_MS,
                )
                await page.wait_for_timeout(
                    1800
                )
                state = await _marketing_state(
                    page
                )
            except Exception:
                state = "redirected"

            if state == "redirected":
                await _marketing_screen(
                    shop_id,
                    page,
                )
                return (
                    "redirected",
                    "После отправки логина Kaspi увёл браузер на другую "
                    f"страницу: {redirected_url}. "
                    "Авторизацию успешной не считаю.",
                )

        await _marketing_screen(shop_id, page)

        if state == "connected":
            await _marketing_save_storage(shop_id, context)
            await _marketing_close(shop_id)
            return "connected", None

        if state in {"otp", "confirmation"}:
            return state, None

        if state == "login":
            return (
                "login",
                "Мы остались на странице "
                "https://marketing.kaspi.kz/sign-in — "
                "вход не завершён. Проверьте логин/пароль.",
            )

        return state, "Kaspi Marketing не подтвердил вход."


async def marketing_submit_code(shop_id, code):
    active = ACTIVE_MARKETING.get(shop_id)
    if not active:
        return "expired", "Сессия авторизации закрыта."

    page = active["page"]
    inp = await _first_visible(
        page,
        [
            'input[autocomplete="one-time-code"]',
            'input[name*="otp"]',
            'input[id*="otp"]',
            'input[name*="code"]',
            'input[id*="code"]',
            'input[type="tel"]',
            'input[type="text"]',
        ],
    )
    if not inp:
        return await _marketing_state(page), None

    await inp.fill(str(code).strip())
    await _marketing_submit(page)
    await page.wait_for_timeout(2500)
    state = await _marketing_state(page)
    await _marketing_screen(shop_id, page)

    if state == "connected":
        await _marketing_save_storage(shop_id, active["context"])
        await _marketing_close(shop_id)

    return state, None


async def marketing_check_confirmation(shop_id):
    active = ACTIVE_MARKETING.get(shop_id)
    if not active:
        if marketing_storage_file(shop_id).exists():
            return "connected", None
        return "expired", "Сессия подтверждения закрыта."

    page = active["page"]
    await page.wait_for_timeout(1200)
    state = await _marketing_state(page)
    await _marketing_screen(shop_id, page)

    if state == "connected":
        await _marketing_save_storage(shop_id, active["context"])
        await _marketing_close(shop_id)

    return state, None


async def _marketing_open_page(shop_id):
    context = await _marketing_context(
        shop_id
    )
    page = await context.new_page()

    await page.goto(
        KASPI_MARKETING_HOME_URL,
        wait_until="domcontentloaded",
        timeout=MARKETING_BROWSER_TIMEOUT_MS,
    )
    await page.wait_for_timeout(1400)

    return context, page


KASPI_PROFILE_SELECTOR = ".mrk-desktop-header-profile"
KASPI_PROFILE_CURRENT_SELECTOR = ".mrk-desktop-header-profile__current"
KASPI_PROFILE_DROPDOWN_SELECTOR = ".mrk-desktop-header-profile__dropdown"
KASPI_PROFILE_ITEM_SELECTOR = (
    ".mrk-desktop-header-profile__dropdown-list "
    ".mrk-desktop-header-profile__dropdown-item"
)
KASPI_PROFILE_NAME_SELECTOR = ".mrk-desktop-header-profile__name"
KASPI_PROFILE_CAPTION_SELECTOR = ".mrk-desktop-header-profile__caption"


def _marketing_clean_text(value):
    return " ".join(
        str(value or "").split()
    )


def _marketing_collapse_duplicate_label(label):
    label = _marketing_clean_text(
        label
    )

    if not label:
        return label

    parts = label.split()

    for copies in (4, 3, 2):
        if (
            len(parts) >= copies
            and len(parts) % copies == 0
        ):
            size = len(parts) // copies
            chunks = [
                parts[
                    index * size:
                    (index + 1) * size
                ]
                for index in range(copies)
            ]

            if all(
                chunk == chunks[0]
                for chunk in chunks[1:]
            ):
                return " ".join(
                    chunks[0]
                )

    return label


def _marketing_cabinet_display_name(
    name,
    caption,
):
    """
    Human-friendly Telegram label.

    Examples from the real DOM:
      ТОО "AMBRELLO" | ТОО "AMBRELLO"
        -> ТОО "AMBRELLO"

      AMBRELLO | ИП NEOGATIS
        -> AMBRELLO — ИП NEOGATIS
    """
    name = _marketing_clean_text(
        name
    )
    caption = _marketing_clean_text(
        caption
    )

    if not caption:
        return name

    if name.lower() == caption.lower():
        return name

    return f"{name} — {caption}"


async def _marketing_exact_profile_available(
    page,
):
    try:
        profile = page.locator(
            KASPI_PROFILE_SELECTOR
        ).first

        return bool(
            await profile.count()
            and await profile.is_visible()
        )
    except Exception:
        return False


async def _marketing_current_header_label(
    page,
):
    """
    Read only the real current cabinet element.
    Never read the whole profile parent, so an open dropdown cannot pollute it.
    """
    try:
        current = page.locator(
            KASPI_PROFILE_CURRENT_SELECTOR
        ).first

        if not (
            await current.count()
            and await current.is_visible()
        ):
            return None

        # inner_text may include the chevron div but it has no visible text.
        return _marketing_clean_text(
            await current.inner_text()
        )

    except Exception:
        return None


async def _marketing_open_cabinet_dropdown(
    page,
):
    """
    Click the exact current-profile control from Kaspi's real DOM.
    """
    try:
        current = page.locator(
            KASPI_PROFILE_CURRENT_SELECTOR
        ).first

        if not (
            await current.count()
            and await current.is_visible()
        ):
            return None

        await current.click(
            timeout=3500,
        )
        await page.wait_for_timeout(
            450
        )

        dropdown = page.locator(
            KASPI_PROFILE_DROPDOWN_SELECTOR
        ).first

        # Kaspi may keep dropdown in DOM all the time and toggle visibility.
        if not await dropdown.count():
            return None

        return {
            "exact_dom": True,
        }

    except Exception:
        return None


async def _marketing_exact_cabinet_items(
    page,
):
    """
    Return ONLY:
      .mrk-desktop-header-profile__dropdown-list
      .mrk-desktop-header-profile__dropdown-item

    The .log-out row is outside dropdown-list in the provided DOM,
    but we exclude it defensively anyway.
    """
    items = []

    try:
        locators = page.locator(
            KASPI_PROFILE_ITEM_SELECTOR
        )

        for index in range(
            await locators.count()
        ):
            item = locators.nth(
                index
            )

            try:
                classes = (
                    await item.get_attribute(
                        "class"
                    )
                    or ""
                )

                if "log-out" in classes.split():
                    continue

                name_loc = item.locator(
                    KASPI_PROFILE_NAME_SELECTOR
                ).first
                caption_loc = item.locator(
                    KASPI_PROFILE_CAPTION_SELECTOR
                ).first

                name = ""
                caption = ""

                if await name_loc.count():
                    name = _marketing_clean_text(
                        await name_loc.inner_text()
                    )

                if await caption_loc.count():
                    try:
                        caption = _marketing_clean_text(
                            await caption_loc.inner_text()
                        )
                    except Exception:
                        caption = ""

                if not name:
                    continue

                title = _marketing_clean_text(
                    await item.get_attribute(
                        "title"
                    )
                    or ""
                )

                # The title is an excellent stable key:
                # AMBRELLO | ИП NEOGATIS
                # ТОО "AMBRELLO" | ТОО "AMBRELLO"
                key = (
                    title
                    or (
                        f"{name} | {caption}"
                        if caption
                        else name
                    )
                )

                items.append({
                    "key": key,
                    "name": name,
                    "caption": caption,
                    "display_name": (
                        _marketing_cabinet_display_name(
                            name,
                            caption,
                        )
                    ),
                    "title": title,
                    "current": (
                        "active"
                        in classes.split()
                    ),
                    "dom_index": index,
                })

            except Exception:
                pass

    except Exception:
        pass

    return items


async def _marketing_dropdown_options(
    page,
    anchor_box=None,
):
    # Exact real DOM only.
    items = await _marketing_exact_cabinet_items(
        page
    )

    return [
        {
            "text": item[
                "display_name"
            ],
            "key": item[
                "key"
            ],
            "name": item[
                "name"
            ],
            "caption": item[
                "caption"
            ],
            "current": item[
                "current"
            ],
            "dom_index": item[
                "dom_index"
            ],
        }
        for item in items
    ]


async def _marketing_click_dropdown_option(
    page,
    anchor_box,
    target_key,
):
    """
    Click the exact dropdown item by the stable title/key captured during
    discovery. No global page text search.
    """
    target_key = _marketing_clean_text(
        target_key
    )

    items = await _marketing_exact_cabinet_items(
        page
    )

    target = None

    for item in items:
        if _marketing_clean_text(
            item["key"]
        ) == target_key:
            target = item
            break

    if not target:
        return False

    try:
        locator = page.locator(
            KASPI_PROFILE_ITEM_SELECTOR
        ).nth(
            int(
                target[
                    "dom_index"
                ]
            )
        )

        if not (
            await locator.count()
            and await locator.is_visible()
        ):
            return False

        await locator.click(
            timeout=4000,
        )
        return True

    except Exception:
        return False


async def marketing_discover_cabinets(
    shop_id,
):
    """
    Discover cabinets using the exact Kaspi Marketing profile DOM supplied
    from the live page.
    """
    context, page = await _marketing_open_page(
        shop_id
    )

    try:
        if await _marketing_state(
            page
        ) != "connected":
            await _marketing_screen(
                shop_id,
                page,
            )
            return []

        if not await _marketing_exact_profile_available(
            page
        ):
            await _marketing_screen(
                shop_id,
                page,
            )
            return []

        opened = await _marketing_open_cabinet_dropdown(
            page
        )

        if not opened:
            await _marketing_screen(
                shop_id,
                page,
            )
            return []

        items = await _marketing_exact_cabinet_items(
            page
        )

        result = []

        for item in items:
            result.append({
                "key": item[
                    "key"
                ],
                "name": item[
                    "display_name"
                ],
                "raw_name": item[
                    "name"
                ],
                "caption": item[
                    "caption"
                ],
                "mode": "exact_kaspi_dom",
                "current": item[
                    "current"
                ],
            })

        await _marketing_screen(
            shop_id,
            page,
        )

        return result[:50]

    finally:
        await context.close()


async def marketing_select_cabinet_on_page(
    page,
    cabinet_name,
    cabinet_key=None,
):
    """
    Switch using exact Kaspi dropdown item, then verify the `.active` item.
    """
    target_key = _marketing_clean_text(
        cabinet_key
        or cabinet_name
    )

    if not target_key:
        return False

    opened = await _marketing_open_cabinet_dropdown(
        page
    )

    if not opened:
        return False

    items = await _marketing_exact_cabinet_items(
        page
    )

    selected = None

    for item in items:
        if _marketing_clean_text(
            item["key"]
        ) == target_key:
            selected = item
            break

    if not selected:
        return False

    # Already active.
    if selected["current"]:
        # Close the dropdown so later navigation isn't obstructed.
        try:
            current = page.locator(
                KASPI_PROFILE_CURRENT_SELECTOR
            ).first
            await current.click(
                timeout=2500,
            )
            await page.wait_for_timeout(
                250
            )
        except Exception:
            pass

        return True

    clicked = await _marketing_click_dropdown_option(
        page,
        None,
        target_key,
    )

    if not clicked:
        return False

    # Wait for Kaspi to change active merchant and rerender.
    await page.wait_for_timeout(
        1200
    )

    try:
        await page.wait_for_load_state(
            "domcontentloaded",
            timeout=5000,
        )
    except Exception:
        pass

    for _ in range(6):
        # Open menu, then verify which exact item has .active.
        opened = await _marketing_open_cabinet_dropdown(
            page
        )

        if opened:
            refreshed = await _marketing_exact_cabinet_items(
                page
            )

            for item in refreshed:
                if (
                    _marketing_clean_text(
                        item["key"]
                    )
                    == target_key
                    and item[
                        "current"
                    ]
                ):
                    # Close dropdown.
                    try:
                        current = page.locator(
                            KASPI_PROFILE_CURRENT_SELECTOR
                        ).first
                        await current.click(
                            timeout=2500,
                        )
                    except Exception:
                        pass

                    return True

            # Close before the next retry.
            try:
                current = page.locator(
                    KASPI_PROFILE_CURRENT_SELECTOR
                ).first
                await current.click(
                    timeout=2500,
                )
            except Exception:
                pass

        await page.wait_for_timeout(
            650
        )

    return False


async def marketing_apply_saved_cabinet(shop_id, page):
    account = DB.marketing_account(shop_id)
    if not account:
        return False

    name = account["selected_cabinet_name"]
    key = account["selected_cabinet_key"]
    if not name:
        return False

    return await marketing_select_cabinet_on_page(page, name, key)


async def marketing_select_cabinet(shop_id, cabinet):
    context, page = await _marketing_open_page(shop_id)
    try:
        ok = await marketing_select_cabinet_on_page(
            page,
            cabinet.get("name"),
            cabinet.get("key"),
        )
        if ok:
            await _marketing_save_storage(shop_id, context)
            await _marketing_screen(shop_id, page)
        return ok
    finally:
        await context.close()


async def marketing_api_json(
    shop_id,
    url,
):
    """Authenticated JSON GET using the saved Kaspi Marketing session."""
    context = await _marketing_context(
        shop_id
    )

    try:
        response = await context.request.get(
            url,
            timeout=MARKETING_BROWSER_TIMEOUT_MS,
            fail_on_status_code=False,
        )

        if not response.ok:
            return None, (
                f"HTTP {response.status}: "
                f"{(await response.text())[:400]}"
            )

        try:
            return await response.json(), None
        except Exception:
            return None, (
                "Ответ Marketing не является JSON: "
                + (await response.text())[:400]
            )

    finally:
        await context.close()


def _marketing_normalize_identity(value):
    return re.sub(
        r"[^a-zа-я0-9]+",
        "",
        str(value or "").lower(),
    )


def _marketing_profile_merchant_candidates(value, selected_name):
    """Find merchant-like ids in profile objects matching selected cabinet."""
    selected_norm = _marketing_normalize_identity(
        selected_name
    )
    results = []

    def walk(obj):
        if isinstance(obj, dict):
            text_blob = " ".join(
                str(v)
                for v in obj.values()
                if isinstance(v, (str, int, float))
            )
            blob_norm = _marketing_normalize_identity(
                text_blob
            )

            identity_matches = (
                not selected_norm
                or selected_norm in blob_norm
                or blob_norm in selected_norm
            )

            if identity_matches:
                for key, val in obj.items():
                    key_low = str(key).lower()
                    raw = str(val or "").strip()

                    if (
                        re.fullmatch(r"\d+", raw)
                        and "merchant" in key_low
                        and "business" not in key_low
                    ):
                        results.append(raw)

                # In a matched profile object, a plain numeric id can also be
                # the merchant id. Keep it only behind explicit merchant keys.
                for key in ("merchantId", "merchant_id"):
                    val = obj.get(key)
                    raw = str(val or "").strip()
                    if re.fullmatch(r"\d+", raw):
                        results.append(raw)

            for child in obj.values():
                if isinstance(child, (dict, list)):
                    walk(child)

        elif isinstance(obj, list):
            for child in obj:
                walk(child)

    walk(value)

    dedup = []
    seen = set()
    for item in results:
        if item not in seen:
            seen.add(item)
            dedup.append(item)
    return dedup


async def _marketing_merchant_id_from_browser(shop_id):
    """
    Resolve the real Marketing merchant id from authenticated resource URLs.

    Important: `accounts/api/User/getdata -> shop.merchantBusinessId` is a
    different business identifier and must NOT be used in Marketing API URLs.
    """
    context, page = await _marketing_open_page(
        shop_id
    )

    try:
        if await _marketing_state(page) != "connected":
            return None

        if not await marketing_apply_saved_cabinet(
            shop_id,
            page,
        ):
            return None

        await page.wait_for_timeout(700)

        # These pages are known to issue /merchant/<real-id>/ API requests
        # even when there are no active campaigns.
        probe_urls = (
            "https://marketing.kaspi.kz/bonuses/products/promotions/overview",
            "https://marketing.kaspi.kz/external/advertising/products/campaigns?tab=list&activeTab=All",
            "https://marketing.kaspi.kz/advertising/campaigns?tab=campaigns&activeTab=Enabled",
        )

        for probe_url in probe_urls:
            try:
                await page.goto(
                    probe_url,
                    wait_until="domcontentloaded",
                    timeout=MARKETING_BROWSER_TIMEOUT_MS,
                )
                await page.wait_for_timeout(1200)

                merchant_id = await marketing_discover_merchant_id_from_page(
                    page
                )
                if merchant_id:
                    return merchant_id
            except Exception:
                continue

        return None

    finally:
        await context.close()


async def marketing_current_merchant_id(
    shop_id,
    force_refresh=False,
):
    account = DB.marketing_account(
        shop_id
    )

    stored_id = (
        str(account["selected_merchant_id"])
        if account and account["selected_merchant_id"]
        else None
    )

    if stored_id and not force_refresh:
        return stored_id

    # First choice: actual authenticated Marketing network traffic.
    try:
        network_id = await _marketing_merchant_id_from_browser(
            shop_id
        )
    except Exception as exc:
        print(
            "Marketing merchant network resolve failed:",
            repr(exc),
        )
        network_id = None

    if network_id:
        DB.set_marketing_merchant_id(
            shop_id,
            network_id,
        )
        print(
            "Marketing merchant id resolved from Marketing API traffic:",
            network_id,
        )
        return network_id

    # Second choice: match the selected cabinet against profile/merchant lists
    # in User/getdata. Never use shop.merchantBusinessId here.
    payload, error = await marketing_api_json(
        shop_id,
        "https://marketing.kaspi.kz/accounts/api/User/getdata",
    )

    if payload:
        data = (
            payload.get("data")
            if isinstance(payload, dict)
            else None
        )

        if isinstance(data, dict):
            selected_name = (
                account["selected_cabinet_name"]
                if account
                else ""
            )

            candidates = []
            candidates.extend(
                _marketing_profile_merchant_candidates(
                    data.get("profiles"),
                    selected_name,
                )
            )
            candidates.extend(
                _marketing_profile_merchant_candidates(
                    data.get("shop", {}).get("merchants")
                    if isinstance(data.get("shop"), dict)
                    else None,
                    selected_name,
                )
            )

            for raw in candidates:
                if re.fullmatch(r"\d+", raw):
                    DB.set_marketing_merchant_id(
                        shop_id,
                        raw,
                    )
                    print(
                        "Marketing merchant id resolved from selected profile:",
                        raw,
                    )
                    return raw

    # Last resort: an already-known id is safer than merchantBusinessId.
    if stored_id:
        print(
            "Marketing merchant id fallback to stored value:",
            stored_id,
        )
        return stored_id

    print(
        "Marketing merchant id not resolved; merchantBusinessId intentionally ignored"
    )
    return None


MARKETING_ACTIVE_CAMPAIGN_STATES = {
    "enabled",
    "active",
    "running",
    "started",
    "live",
    "on",
}

MARKETING_INACTIVE_CAMPAIGN_STATES = {
    "paused",
    "pause",
    "disabled",
    "deleted",
    "removed",
    "archived",
    "archive",
    "ended",
    "finished",
    "stopped",
    "inactive",
    "draft",
    "pending",
    "pausedbysegment",
    "paused_by_segment",
}


def marketing_campaign_state(obj):
    if not isinstance(obj, dict):
        return None

    lowered = {
        str(key).lower(): value
        for key, value in obj.items()
    }

    for key in (
        "state",
        "status",
        "campaignstate",
        "campaign_state",
    ):
        value = lowered.get(key)
        if value not in (None, ""):
            return str(value).strip()

    return None


def marketing_campaign_is_active(obj):
    """
    Strict mode: advertising campaign is synchronized only when Kaspi
    explicitly says it is active/enabled/running.

    Unknown states are NOT treated as active. This prevents old/paused/
    deleted campaigns from accidentally entering the download queue.
    """
    state = marketing_campaign_state(
        obj
    )

    if not state:
        return False

    normalized = re.sub(
        r"[^a-zа-я0-9]+",
        "",
        state.lower(),
    )

    if normalized in {
        re.sub(r"[^a-zа-я0-9]+", "", value)
        for value in MARKETING_ACTIVE_CAMPAIGN_STATES
    }:
        return True

    return False


MARKETING_ACTIVITY_KEYWORDS = {
    "view",
    "views",
    "impression",
    "impressions",
    "show",
    "shows",
    "click",
    "clicks",
    "favorite",
    "favorites",
    "favourite",
    "favourites",
    "cart",
    "basket",
    "addtocart",
    "add_to_cart",
    "orders",
    "ordercount",
    "order_count",
    "transactions",
    "transactioncount",
    "transaction_count",
    "cost",
    "spend",
    "expense",
    "expenses",
    "advertisingcost",
    "advertising_cost",
}


def _marketing_float(value):
    if value is None:
        return 0.0

    if isinstance(value, bool):
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    raw = str(value).strip()
    if not raw:
        return 0.0

    raw = (
        raw.replace("\\xa0", "")
        .replace(" ", "")
        .replace("₸", "")
        .replace("тг", "")
        .replace(",", ".")
    )

    raw = re.sub(
        r"[^0-9.\\-]",
        "",
        raw,
    )

    if raw in {"", "-", ".", "-."}:
        return 0.0

    try:
        return float(raw)
    except Exception:
        return 0.0


def marketing_campaign_recent_activity(obj):
    """
    Campaign-list endpoints aggregate metrics for the requested date range.
    We call them for exactly the last N days, so any positive metric here
    means the campaign had recent activity in that window.

    Returns a dict with matched metrics and total positive signal.
    """
    matched = {}

    def walk(value, depth=0):
        if depth > 3:
            return

        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(
                    r"[^a-z0-9]+",
                    "",
                    str(key).lower(),
                )

                keyword_match = False
                for keyword in MARKETING_ACTIVITY_KEYWORDS:
                    normalized_keyword = re.sub(
                        r"[^a-z0-9]+",
                        "",
                        keyword.lower(),
                    )
                    if normalized == normalized_keyword:
                        keyword_match = True
                        break

                if keyword_match:
                    number = _marketing_float(child)
                    if number > 0:
                        matched[str(key)] = number

                if isinstance(child, (dict, list)):
                    walk(child, depth + 1)

        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)

    walk(obj)

    return {
        "has_activity": bool(matched),
        "metrics": matched,
        "signal": sum(matched.values()),
    }


def marketing_campaign_eligibility_window():
    today = current_business_date()
    days = max(
        1,
        int(MARKETING_RECENT_ACTIVITY_DAYS),
    )
    start = today - timedelta(days=days - 1)
    return start, today


def marketing_campaign_list_url(
    section_key,
    merchant_id,
    start_date,
    end_date,
    state=None,
):
    start_s = start_date.isoformat()
    end_s = end_date.isoformat()
    base = "https://marketing.kaspi.kz"

    if section_key == "external":
        return (
            f"{base}/external/advertising/products/api/v1/"
            f"merchant/{merchant_id}/campaigns"
            f"?startDate={start_s}&endDate={end_s}"
        )

    if section_key == "goods":
        url = (
            f"{base}/advertising/products/api/v5/"
            f"merchant/{merchant_id}/Campaigns"
            f"?StartDate={start_s}&EndDate={end_s}"
        )
        if state:
            url += f"&state={state}"
        return url

    return None


async def marketing_direct_campaign_records(
    shop_id,
    section_key,
):
    """
    Directly load campaign lists for sections whose exact Kaspi endpoints
    have already been observed in the live browser network log.
    """
    if section_key not in {
        "external",
        "goods",
    }:
        return []

    merchant_id = await marketing_current_merchant_id(
        shop_id
    )

    if not merchant_id:
        return []

    # Eligibility is always based on recent activity only. Do not use the
    # initial 90-day report lookback here, otherwise old paused campaigns
    # with historical metrics would become eligible again.
    start_date, end_date = marketing_campaign_eligibility_window()

    urls = []

    if section_key == "external":
        # External endpoint returns all states for the requested recent window.
        urls.append(
            marketing_campaign_list_url(
                section_key,
                merchant_id,
                start_date,
                end_date,
            )
        )
    else:
        # We intentionally request all states for exactly the recent-activity
        # window. This lets us include paused/deleted campaigns only when they
        # still had views/clicks/spend during the last N days.
        urls.append(
            marketing_campaign_list_url(
                section_key,
                merchant_id,
                start_date,
                end_date,
            )
        )
        # If Kaspi rejects the unfiltered variant, active campaigns must still
        # synchronize.
        urls.append(
            marketing_campaign_list_url(
                section_key,
                merchant_id,
                start_date,
                end_date,
                state="Enabled",
            )
        )

    for url in urls:
        payload, error = await marketing_api_json(
            shop_id,
            url,
        )

        if not payload:
            print(
                "Marketing direct campaign list failed:",
                section_key,
                url,
                error,
            )
            continue

        data = (
            payload.get("data")
            if isinstance(payload, dict)
            else None
        )

        if not isinstance(data, list):
            print(
                "Marketing direct campaign list unexpected shape:",
                section_key,
                url,
                type(data).__name__,
            )
            continue

        records = []
        seen = set()
        state_counts = {}
        active_count = 0
        recent_inactive_count = 0
        skipped_count = 0

        for obj in data:
            state = marketing_campaign_state(
                obj
            )
            state_key = (
                str(state).strip()
                if state not in (None, "")
                else "<missing>"
            )
            state_counts[state_key] = (
                state_counts.get(
                    state_key,
                    0,
                )
                + 1
            )

            is_active = marketing_campaign_is_active(
                obj
            )
            activity = marketing_campaign_recent_activity(
                obj
            )

            # Core rule:
            #   active -> always sync
            #   inactive -> sync only if it had any activity in last N days
            if not is_active and not activity["has_activity"]:
                skipped_count += 1
                continue

            record = _marketing_campaign_record_from_json_obj(
                obj,
                merchant_id=merchant_id,
            )

            if not record:
                skipped_count += 1
                continue

            if is_active:
                record["sync_reason"] = "active"
                active_count += 1
            else:
                record["sync_reason"] = "recent_activity"
                record["recent_activity"] = activity["metrics"]
                recent_inactive_count += 1

            marker = (
                record["campaign_id"],
                record["name"].lower(),
            )

            if marker in seen:
                continue

            seen.add(marker)
            records.append(record)

        print(
            "Marketing campaign eligibility:",
            section_key,
            "merchant=",
            merchant_id,
            "window=",
            f"{start_date}..{end_date}",
            "total=",
            len(data),
            "active=",
            active_count,
            "recent_inactive=",
            recent_inactive_count,
            "skipped=",
            skipped_count,
            "states=",
            state_counts,
        )

        # Valid API response with zero eligible campaigns is still success.
        # Never fall back to DOM merely to rediscover old campaigns.
        return records

    # None means direct API failed completely; caller may use UI fallback.
    return None


async def marketing_direct_campaign_records_for_period(
    shop_id,
    section_key,
    start_date,
    end_date,
):
    """
    Return only campaigns that matter for the requested period:
    active campaigns, or inactive campaigns that actually had activity/spend
    inside that exact period.
    """
    if section_key not in {
        "external",
        "goods",
    }:
        return None

    merchant_id = await marketing_current_merchant_id(
        shop_id
    )

    if not merchant_id:
        return None

    urls = [
        marketing_campaign_list_url(
            section_key,
            merchant_id,
            start_date,
            end_date,
        )
    ]

    # Enabled fallback keeps currently active product ads available if Kaspi
    # ever rejects the unfiltered goods endpoint.
    if section_key == "goods":
        urls.append(
            marketing_campaign_list_url(
                section_key,
                merchant_id,
                start_date,
                end_date,
                state="Enabled",
            )
        )

    for url in urls:
        payload, error = await marketing_api_json(
            shop_id,
            url,
        )

        if not payload:
            continue

        data = (
            payload.get("data")
            if isinstance(payload, dict)
            else None
        )

        if not isinstance(data, list):
            continue

        records = []
        seen = set()

        period_includes_today = (
            start_date
            <= current_business_date()
            <= end_date
        )

        for obj in data:
            is_active = marketing_campaign_is_active(
                obj
            )
            activity = marketing_campaign_recent_activity(
                obj
            )

            # For historical dates, do not download every campaign that happens
            # to be active today. It must have had activity/spend in the
            # requested historical period. If the range includes today, current
            # active campaigns are included even before they accumulate metrics.
            if not activity["has_activity"]:
                if not (
                    period_includes_today
                    and is_active
                ):
                    continue

            record = _marketing_campaign_record_from_json_obj(
                obj,
                merchant_id=merchant_id,
            )
            if not record:
                continue

            record["sync_reason"] = (
                "active"
                if is_active
                else "period_activity"
            )

            marker = (
                record["campaign_id"],
                record["name"].lower(),
            )
            if marker in seen:
                continue

            seen.add(marker)
            records.append(record)

        print(
            "Marketing period campaigns:",
            section_key,
            f"{start_date}..{end_date}",
            "count=",
            len(records),
        )
        return records

    return None


async def marketing_ad_activity_plan(
    shop_id,
    section_key,
    start_date,
    end_date,
):
    if section_key not in MARKETING_ACQUISITION_SECTIONS:
        return {}, {}

    merchant_id = await marketing_current_merchant_id(
        shop_id
    )

    if not merchant_id:
        return {}, {}

    campaign_plan = {}
    daily_expected_cost = {}

    for calendar_day in _date_sequence(
        start_date,
        end_date + timedelta(days=1),
    ):
        urls = [
            marketing_campaign_list_url(
                section_key,
                merchant_id,
                calendar_day,
                calendar_day,
            )
        ]

        if section_key == "goods":
            urls.append(
                marketing_campaign_list_url(
                    section_key,
                    merchant_id,
                    calendar_day,
                    calendar_day,
                    state="Enabled",
                )
            )

        data = None

        for url in urls:
            payload, error = await marketing_api_json(
                shop_id,
                url,
            )

            candidate = (
                payload.get("data")
                if isinstance(payload, dict)
                else None
            )

            if isinstance(candidate, list):
                data = candidate
                break

        if data is None:
            continue

        calendar_cost = 0.0

        for obj in data:
            record = _marketing_campaign_record_from_json_obj(
                obj,
                merchant_id=merchant_id,
            )

            if not record:
                continue

            activity = marketing_campaign_recent_activity(
                obj
            )
            cost = float(
                record.get("cost")
                or 0
            )

            if (
                not activity["has_activity"]
                and abs(cost) < 0.000001
            ):
                continue

            marker = (
                record["campaign_id"],
                record["name"].lower(),
            )

            bucket = campaign_plan.setdefault(
                marker,
                {
                    "campaign": record,
                    "days": set(),
                },
            )
            bucket["days"].add(
                calendar_day
            )
            calendar_cost += cost

        daily_expected_cost[
            calendar_day.isoformat()
        ] = calendar_cost

    print(
        "Marketing activity plan:",
        section_key,
        "campaigns=",
        len(campaign_plan),
        "calendar_days=",
        len(daily_expected_cost),
        "expected_cost=",
        sum(daily_expected_cost.values()),
    )

    return campaign_plan, daily_expected_cost


def marketing_bonus_campaign_list_urls(
    section_key,
    merchant_id,
    start_date,
    end_date,
):
    base = "https://marketing.kaspi.kz"
    start_s = start_date.isoformat()
    end_s = end_date.isoformat()

    if section_key == "bonus_purchase":
        root = (
            f"{base}/bonuses/products/api/v3/merchant/"
            f"{merchant_id}/campaigns"
        )
    elif section_key == "bonus_review":
        root = (
            f"{base}/bonuses/reviews/api/v3/merchant/"
            f"{merchant_id}/campaigns"
        )
    else:
        return []

    return [
        f"{root}?StartDate={start_s}&EndDate={end_s}",
        f"{root}?startDate={start_s}&endDate={end_s}",
        root,
    ]


async def marketing_direct_bonus_campaign_records_for_period(
    shop_id,
    section_key,
    start_date,
    end_date,
):
    merchant_id = await marketing_current_merchant_id(
        shop_id
    )

    if not merchant_id:
        return []

    for url in marketing_bonus_campaign_list_urls(
        section_key,
        merchant_id,
        start_date,
        end_date,
    ):
        payload, error = await marketing_api_json(
            shop_id,
            url,
        )

        if not payload:
            continue

        records = _marketing_walk_json_campaigns(
            payload,
            merchant_id=merchant_id,
        )

        if records:
            print(
                "Marketing bonus campaigns direct:",
                section_key,
                "count=",
                len(records),
                "url=",
                url,
            )
            return records

    return []


def marketing_bonus_overview_url(
    section_key,
    merchant_id,
    report_day,
):
    base = "https://marketing.kaspi.kz"
    day = report_day.isoformat()

    if section_key == "bonus_purchase":
        return (
            f"{base}/bonuses/products/api/v1/merchant/{merchant_id}/overview"
            f"?StartDate={day}&EndDate={day}"
        )

    if section_key == "bonus_review":
        return (
            f"{base}/bonuses/reviews/api/v3/merchant/{merchant_id}/overview"
            f"?StartDate={day}&EndDate={day}"
        )

    return None


async def marketing_sync_bonus_overview_period(
    shop_id,
    cabinet_key,
    cabinet_name,
    section_key,
    section_name,
    days,
    sync_run_id,
):
    """
    Reliable fallback confirmed by live Kaspi traffic.

    Product bonus overview exposes bonusAmount/views/clicks/transactions/gmv.
    Review bonus overview exposes bonusAmount/notifications/reviews.

    These rows are section totals (unallocated to a SKU) but they guarantee the
    actual bonus expense is included in daily Marketing spend and net profit.
    """
    merchant_id = await marketing_current_merchant_id(
        shop_id
    )

    if not merchant_id:
        return 0, 0, ["merchant id не найден"]

    context = await _marketing_context(
        shop_id
    )

    files = 0
    rows = 0
    errors = []
    campaign_name = (
        "Бонус от продавца"
        if section_key == "bonus_purchase"
        else "Бонус за отзыв"
    )

    try:
        for report_day in days:
            url = marketing_bonus_overview_url(
                section_key,
                merchant_id,
                report_day,
            )

            if not url:
                continue

            try:
                response = await context.request.get(
                    url,
                    timeout=MARKETING_BROWSER_TIMEOUT_MS,
                    fail_on_status_code=False,
                )

                if not response.ok:
                    errors.append(
                        f"{report_day}: HTTP {response.status}"
                    )
                    continue

                payload = await response.json()
            except Exception as exc:
                errors.append(
                    f"{report_day}: {exc}"
                )
                continue

            data = payload
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                data = payload["data"]

            if not isinstance(data, dict):
                errors.append(
                    f"{report_day}: неожиданный JSON"
                )
                continue

            amount = _marketing_float(
                data.get("bonusAmount")
            )

            # Exact refresh is idempotent: clear stale value first, even when
            # Kaspi now reports zero.
            DB.delete_marketing_report_slice(
                shop_id,
                cabinet_key,
                section_key,
                campaign_name,
                report_day.isoformat(),
            )

            activity_values = [
                data.get("views"),
                data.get("clicks"),
                data.get("transactions"),
                data.get("notifications"),
                data.get("reviews"),
                data.get("gmv"),
            ]
            has_activity = any(
                _marketing_float(value) > 0
                for value in activity_values
            )

            if amount <= 0 and not has_activity:
                files += 1
                continue

            raw_payload = {
                "date": report_day.isoformat(),
                "source": "bonus_overview",
                **data,
            }

            fingerprint_payload = {
                "cabinet_key": cabinet_key,
                "section_key": section_key,
                "campaign_name": campaign_name,
                "date": report_day.isoformat(),
                "amount": amount,
            }
            fingerprint = hashlib.sha256(
                json.dumps(
                    fingerprint_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()

            DB.upsert_marketing_report_row(
                shop_id=shop_id,
                cabinet_key=cabinet_key,
                cabinet_name=cabinet_name,
                section_key=section_key,
                section_name=section_name,
                campaign_name=campaign_name,
                report_date=report_day.isoformat(),
                product_code=None,
                sku=None,
                product_name=None,
                order_code=None,
                amount=amount,
                clicks=(
                    _marketing_float(data.get("clicks"))
                    if data.get("clicks") is not None
                    else None
                ),
                impressions=(
                    _marketing_float(data.get("views"))
                    if data.get("views") is not None
                    else _marketing_float(data.get("notifications"))
                    if data.get("notifications") is not None
                    else None
                ),
                orders_count=(
                    _marketing_float(data.get("transactions"))
                    if data.get("transactions") is not None
                    else None
                ),
                revenue=(
                    _marketing_float(data.get("gmv"))
                    if data.get("gmv") is not None
                    else None
                ),
                mapping_quality="section_total",
                source_file=f"api_overview_{report_day.isoformat()}",
                fingerprint=fingerprint,
                sync_run_id=sync_run_id,
                raw_json=json.dumps(
                    raw_payload,
                    ensure_ascii=False,
                    default=str,
                ),
            )
            rows += 1
            files += 1

        return files, rows, errors

    finally:
        await context.close()


def marketing_extract_merchant_id(value):
    if not value:
        return None

    match = re.search(
        r"/merchant/(\d+)(?:/|$)",
        str(value),
        re.I,
    )
    return match.group(1) if match else None


def marketing_extract_campaign_id(value):
    if not value:
        return None

    raw = str(value)

    # campaignId=123
    match = re.search(
        r"[?&]campaignId=(\d+)",
        raw,
        re.I,
    )
    if match:
        return match.group(1)

    # /campaigns/123/
    match = re.search(
        r"/campaigns/(\d+)(?:/|$)",
        raw,
        re.I,
    )
    if match:
        return match.group(1)

    # /campaign/123/
    match = re.search(
        r"/campaign/(\d+)(?:/|$)",
        raw,
        re.I,
    )
    return match.group(1) if match else None


def marketing_direct_report_url(
    section_key,
    merchant_id,
    campaign_id,
    start_date,
    end_date,
):
    """
    Exact internal report routes observed in Kaspi Marketing.
    These URLs still require the authenticated Marketing session.
    """
    if not merchant_id or not campaign_id:
        return None

    base = "https://marketing.kaspi.kz"
    start_s = start_date.isoformat()
    end_s = end_date.isoformat()

    if section_key == "external":
        return (
            f"{base}/external/advertising/products/api/v1/"
            f"merchant/{merchant_id}/reports/products/xlsx"
            f"?campaignId={campaign_id}"
            f"&startDate={start_s}&endDate={end_s}&culture=ru-RU"
        )

    if section_key == "goods":
        return (
            f"{base}/advertising/products/api/v3/"
            f"merchant/{merchant_id}/reports/products/csv"
            f"?campaignId={campaign_id}"
            f"&startDate={start_s}&endDate={end_s}"
        )

    if section_key == "bonus_purchase":
        return (
            f"{base}/bonuses/products/api/v1/"
            f"merchant/{merchant_id}/campaigns/{campaign_id}/"
            f"products/report/csv"
            f"?startDate={start_s}&endDate={end_s}&culture=ru-RU"
        )

    if section_key == "bonus_review":
        return (
            f"{base}/bonuses/reviews/api/v3/"
            f"merchant/{merchant_id}/campaigns/{campaign_id}/"
            f"products/report/xlsx"
            f"?startDate={start_s}&endDate={end_s}&culture=ru-RU"
        )

    return None


def marketing_report_file_kind(
    section_key,
):
    if section_key in {
        "goods",
        "bonus_purchase",
    }:
        return "csv"
    if section_key in {
        "external",
        "bonus_review",
    }:
        return "xlsx"
    return None


async def marketing_discover_merchant_id_from_page(
    page,
):
    urls = []

    try:
        urls.extend(
            await page.evaluate(
                """() => performance.getEntriesByType('resource')
                    .map(x => x.name)
                    .filter(Boolean)
                    .slice(-500)"""
            )
        )
    except Exception:
        pass

    try:
        urls.append(
            page.url
        )
    except Exception:
        urls.append(
            page.url
            if hasattr(page, "url")
            else ""
        )

    try:
        html_text = await page.content()
        match = re.search(
            r"/merchant/(\d+)(?:/|\\\\/)",
            html_text,
            re.I,
        )
        if match:
            return match.group(1)
    except Exception:
        pass

    for url in urls:
        merchant_id = marketing_extract_merchant_id(
            url
        )
        if merchant_id:
            return merchant_id

    return None


async def marketing_request_report(
    shop_id,
    url,
    context=None,
):
    """
    Use Playwright BrowserContext.request so the request shares the
    encrypted authenticated cookies/session with Marketing.
    """
    own_context = context is None

    if own_context:
        context = await _marketing_context(
            shop_id
        )

    try:
        response = await context.request.get(
            url,
            timeout=MARKETING_BROWSER_TIMEOUT_MS,
            fail_on_status_code=False,
        )

        if not response.ok:
            return (
                None,
                f"HTTP {response.status}: "
                f"{(await response.text())[:300]}",
            )

        payload = await response.body()

        filename = None
        disposition = response.headers.get(
            "content-disposition",
            "",
        )
        match = re.search(
            r"filename\\*?=(?:UTF-8''|utf-8'')?\"?([^\";]+)",
            disposition,
            re.I,
        )
        if match:
            filename = match.group(1).strip()

        if not filename:
            filename = (
                "marketing_report.csv"
                if "/csv" in url
                else "marketing_report.xlsx"
            )

        return (
            (filename, payload, url),
            None,
        )

    finally:
        if own_context:
            await context.close()


MARKETING_REPORT_SECTIONS = {
    "external": "Внешняя реклама",
    "goods": "Реклама товаров",
    "bonus_purchase": "Бонус от продавца",
    "bonus_review": "Бонус за отзыв",
}

MARKETING_SECTION_ENTRY_BUTTONS = {
    "external": ("Запустить рекламу",),
    "goods": ("Начать рекламировать",),
    "bonus_purchase": ("Запустить акцию",),
    "bonus_review": ("Запустить акцию",),
}


async def marketing_click_section_card(page, section_key):
    """
    Real Kaspi Marketing dashboard navigation:
    find the section card by title, then click the button inside that same card.
    """
    title_text = MARKETING_REPORT_SECTIONS[section_key]
    button_texts = MARKETING_SECTION_ENTRY_BUTTONS.get(
        section_key,
        (),
    )

    # DOM-first approach: it is much more reliable for cards than searching
    # the button globally because several cards share the same button text.
    try:
        clicked = await page.evaluate(
            r"""
({titleText, buttonTexts}) => {
  const clean = v => (v || '').replace(/\s+/g, ' ').trim();

  const visible = el => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return (
      s.display !== 'none' &&
      s.visibility !== 'hidden' &&
      Number(s.opacity || 1) > 0 &&
      r.width > 20 &&
      r.height > 10
    );
  };

  const titleNodes = Array.from(
    document.querySelectorAll('div,span,p,h1,h2,h3,h4')
  ).filter(el =>
    visible(el) &&
    clean(el.innerText || el.textContent) === titleText
  );

  for (const title of titleNodes) {
    let card = title;

    for (let depth = 0; depth < 6 && card; depth++, card = card.parentElement) {
      const candidates = Array.from(
        card.querySelectorAll('button,a,[role="button"],div,span')
      );

      for (const el of candidates) {
        if (!visible(el)) continue;

        const txt = clean(el.innerText || el.textContent);
        if (!buttonTexts.includes(txt)) continue;

        const clickable =
          el.closest('button,a,[role="button"],[tabindex]') || el;

        clickable.dispatchEvent(
          new MouseEvent('mousedown', {
            bubbles: true,
            cancelable: true,
            view: window
          })
        );
        clickable.dispatchEvent(
          new MouseEvent('mouseup', {
            bubbles: true,
            cancelable: true,
            view: window
          })
        );
        clickable.click();
        return true;
      }
    }
  }

  return false;
}
""",
            {
                "titleText": title_text,
                "buttonTexts": list(button_texts),
            },
        )
    except Exception:
        clicked = False

    if clicked:
        try:
            await page.wait_for_load_state(
                "domcontentloaded",
                timeout=12000,
            )
        except Exception:
            pass
        await page.wait_for_timeout(1200)
        return True

    # Playwright fallback scoped to ancestors of the exact title.
    titles = page.get_by_text(
        title_text,
        exact=True,
    )

    for i in range(min(await titles.count(), 20)):
        title = titles.nth(i)

        try:
            if not await title.is_visible():
                continue
        except Exception:
            continue

        for depth in range(1, 6):
            try:
                card = title.locator(
                    f"xpath=ancestor::div[{depth}]"
                )
                if not await card.count():
                    continue

                for button_text in button_texts:
                    button = card.get_by_text(
                        button_text,
                        exact=True,
                    ).first

                    if (
                        await button.count()
                        and await button.is_visible()
                    ):
                        await button.click()
                        try:
                            await page.wait_for_load_state(
                                "domcontentloaded",
                                timeout=12000,
                            )
                        except Exception:
                            pass
                        await page.wait_for_timeout(1200)
                        return True
            except Exception:
                pass

    return False


async def marketing_open_section_page(
    shop_id,
    section_key,
):
    context, page = await _marketing_open_page(
        shop_id
    )

    if await _marketing_state(page) != "connected":
        await context.close()
        return None, None

    if not await marketing_apply_saved_cabinet(
        shop_id,
        page,
    ):
        await _marketing_screen(
            shop_id,
            page,
        )
        await context.close()
        return None, None

    # Cabinet switch may trigger SPA refresh.
    await page.wait_for_timeout(700)

    opened = await marketing_click_section_card(
        page,
        section_key,
    )

    # Bonus cards sometimes change their button DOM. Fall back to the
    # section href exposed by the Marketing dashboard itself.
    if (
        not opened
        and section_key in {
            "bonus_purchase",
            "bonus_review",
        }
    ):
        href_piece = (
            "/bonuses/products"
            if section_key == "bonus_purchase"
            else "/bonuses/reviews"
        )

        try:
            link = page.locator(
                f'a[href*="{href_piece}"]'
            ).first

            if await link.count():
                href = await link.get_attribute(
                    "href"
                )

                if href:
                    if href.startswith("/"):
                        href = (
                            "https://marketing.kaspi.kz"
                            + href
                        )

                    await page.goto(
                        href,
                        wait_until="domcontentloaded",
                        timeout=MARKETING_BROWSER_TIMEOUT_MS,
                    )
                    await page.wait_for_timeout(
                        1200
                    )
                    opened = True
        except Exception:
            pass

    if not opened:
        await _marketing_screen(
            shop_id,
            page,
        )
        await context.close()
        return None, None

    return context, page


async def marketing_open_campaigns(
    shop_id,
    section_key,
):
    context, page = await marketing_open_section_page(
        shop_id,
        section_key,
    )

    if not context:
        return None, None

    # Advertisement sections expose "Обзор / Мои кампании".
    # Click it only if it exists; bonus/calendar sections can have another UI.
    try:
        campaigns_tab = page.get_by_text(
            "Мои кампании",
            exact=True,
        ).first

        if (
            await campaigns_tab.count()
            and await campaigns_tab.is_visible()
        ):
            await campaigns_tab.click()
            await page.wait_for_timeout(800)

    except Exception:
        pass

    return context, page


def _marketing_campaign_obj_score(obj):
    if not isinstance(obj, dict):
        return 0

    keys = {
        str(key).lower()
        for key in obj.keys()
    }

    score = 0

    if any(
        key in keys
        for key in {
            "campaignid",
            "campaign_id",
            "id",
        }
    ):
        score += 4

    if any(
        key in keys
        for key in {
            "name",
            "title",
            "campaigntitle",
            "campaign_title",
            "campaignname",
            "campaign_name",
            "advertisingcampaignname",
            "advertising_campaign_name",
        }
    ):
        score += 4

    if any(
        key in keys
        for key in {
            "status",
            "state",
            "dailybudget",
            "daily_budget",
            "budget",
            "clicks",
            "orders",
            "expense",
            "expenses",
            "spend",
        }
    ):
        score += 2

    return score


def _marketing_campaign_record_from_json_obj(
    obj,
    merchant_id=None,
):
    if not isinstance(obj, dict):
        return None

    def first(*names):
        lowered = {
            str(key).lower(): value
            for key, value in obj.items()
        }
        for name in names:
            if name.lower() in lowered:
                value = lowered[name.lower()]
                if value not in (None, ""):
                    return value
        return None

    campaign_id = first(
        "campaignId",
        "campaign_id",
        "id",
    )

    name = first(
        "campaignName",
        "campaign_name",
        "campaignTitle",
        "campaign_title",
        "advertisingCampaignName",
        "advertising_campaign_name",
        "name",
        "title",
    )

    if campaign_id in (None, "") or name in (None, ""):
        return None

    campaign_id = str(campaign_id).strip()
    name = " ".join(str(name).split())

    if not re.fullmatch(r"\d+", campaign_id):
        return None

    if not name or len(name) > 200:
        return None

    low = name.lower()

    if low in {
        "реклама",
        "внешняя реклама",
        "реклама товаров",
        "реклама товаров в магазине",
        "реклама для брендов",
        "обзор",
        "мои кампании",
        "все",
    }:
        return None

    return {
        "name": name,
        "href": "",
        "campaign_id": campaign_id,
        "merchant_id": (
            str(merchant_id)
            if merchant_id
            else None
        ),
        "state": first(
            "state",
            "status",
        ),
        "cost": _marketing_float(
            first(
                "cost",
                "spend",
                "expense",
                "advertisingCost",
            )
        ),
        "views": _marketing_float(
            first(
                "views",
                "impressions",
                "shows",
            )
        ),
        "clicks": _marketing_float(
            first(
                "clicks",
            )
        ),
        "transactions": _marketing_float(
            first(
                "transactions",
                "orders",
            )
        ),
        "raw_campaign": obj,
    }


def _marketing_walk_json_campaigns(
    payload,
    merchant_id=None,
):
    """
    Recursively inspect a Marketing JSON response and extract objects that
    look like campaigns. This is deliberately schema-flexible because Kaspi
    can wrap the list in data/content/items/result/etc.
    """
    records = []
    seen = set()

    def walk(value):
        if isinstance(value, dict):
            if _marketing_campaign_obj_score(value) >= 8:
                record = _marketing_campaign_record_from_json_obj(
                    value,
                    merchant_id=merchant_id,
                )
                if record:
                    marker = (
                        record["campaign_id"],
                        record["name"].lower(),
                    )
                    if marker not in seen:
                        seen.add(marker)
                        records.append(record)

            for child in value.values():
                walk(child)

        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return records


def _marketing_json_shape(value, depth=0):
    """
    Compact safe description for logs; does not dump full response bodies.
    """
    if depth >= 2:
        if isinstance(value, dict):
            return {
                "type": "dict",
                "keys": list(value.keys())[:20],
            }
        if isinstance(value, list):
            return {
                "type": "list",
                "len": len(value),
            }
        return type(value).__name__

    if isinstance(value, dict):
        result = {
            "type": "dict",
            "keys": list(value.keys())[:30],
        }

        for key, child in list(value.items())[:8]:
            if isinstance(
                child,
                (dict, list),
            ):
                result[
                    str(key)
                ] = _marketing_json_shape(
                    child,
                    depth + 1,
                )

        return result

    if isinstance(value, list):
        result = {
            "type": "list",
            "len": len(value),
        }

        if value:
            result["first"] = (
                _marketing_json_shape(
                    value[0],
                    depth + 1,
                )
            )

        return result

    return {
        "type": type(value).__name__,
        "value": str(value)[:120],
    }


async def _marketing_campaigns_from_network(
    page,
    section_key,
):
    """
    Probe all relevant Marketing XHR/fetch/JSON traffic.

    Previous build only inspected URLs containing `campaign`, which was too
    narrow. Kaspi can load the campaign list through a merchant/advertising
    endpoint whose URL does not contain that word.
    """
    captured = []
    diagnostics = []
    response_tasks = []
    request_urls = []

    def interesting_url(url):
        low = str(url or "").lower()

        if "marketing.kaspi.kz" not in low:
            return False

        return any(
            token in low
            for token in (
                "/api/",
                "/merchant/",
                "/advertising/",
                "/bonuses/",
                "campaign",
                "product",
            )
        )

    async def inspect_response(response):
        try:
            url = str(
                response.url
                or ""
            )

            if not interesting_url(
                url
            ):
                return

            status = response.status
            content_type = (
                response.headers.get(
                    "content-type",
                    "",
                )
                or ""
            ).lower()

            diag = {
                "url": url,
                "status": status,
                "content_type": content_type,
                "records": 0,
                "shape": None,
            }

            if (
                status < 200
                or status >= 400
            ):
                diagnostics.append(
                    diag
                )
                return

            payload = None

            try:
                payload = await response.json()
            except Exception:
                # Some JSON APIs have an incorrect/missing content-type.
                if any(
                    token in url.lower()
                    for token in (
                        "/api/",
                        "/merchant/",
                        "campaign",
                    )
                ):
                    try:
                        raw_text = await response.text()
                        stripped = raw_text.lstrip()

                        if stripped.startswith(
                            ("{", "[")
                        ):
                            payload = json.loads(
                                raw_text
                            )
                    except Exception:
                        pass

            if payload is None:
                diagnostics.append(
                    diag
                )
                return

            diag["shape"] = (
                _marketing_json_shape(
                    payload
                )
            )

            merchant_id = (
                marketing_extract_merchant_id(
                    url
                )
            )

            records = (
                _marketing_walk_json_campaigns(
                    payload,
                    merchant_id=merchant_id,
                )
            )

            diag["records"] = len(
                records
            )
            diagnostics.append(
                diag
            )

            if records:
                captured.append({
                    "url": url,
                    "merchant_id": merchant_id,
                    "records": records,
                })

        except Exception as exc:
            diagnostics.append({
                "url": getattr(
                    response,
                    "url",
                    "",
                ),
                "status": None,
                "content_type": "",
                "records": 0,
                "shape": {
                    "error": repr(exc),
                },
            })

    def on_response(response):
        task = asyncio.create_task(
            inspect_response(
                response
            )
        )
        response_tasks.append(
            task
        )

    def on_request(request):
        try:
            url = str(
                request.url
                or ""
            )

            if (
                interesting_url(
                    url
                )
                and url not in request_urls
            ):
                request_urls.append(
                    url
                )
        except Exception:
            pass

    page.on(
        "response",
        on_response,
    )
    page.on(
        "request",
        on_request,
    )

    try:
        # Clear previous resource timing entries so diagnostics are focused
        # on this reload.
        try:
            await page.evaluate(
                "() => performance.clearResourceTimings()"
            )
        except Exception:
            pass

        try:
            await page.reload(
                wait_until="domcontentloaded",
                timeout=MARKETING_BROWSER_TIMEOUT_MS,
            )
        except Exception:
            pass

        # Give the Vue app time to issue delayed data requests.
        await page.wait_for_timeout(
            5000
        )

        if response_tasks:
            await asyncio.gather(
                *response_tasks,
                return_exceptions=True,
            )

        # Performance entries catch resources that may have escaped listeners.
        try:
            perf_urls = await page.evaluate(
                """() => performance
                    .getEntriesByType('resource')
                    .map(x => x.name)
                    .filter(Boolean)"""
            )

            for url in perf_urls:
                if (
                    interesting_url(
                        url
                    )
                    and url not in request_urls
                ):
                    request_urls.append(
                        url
                    )
        except Exception:
            pass

    finally:
        try:
            page.remove_listener(
                "response",
                on_response,
            )
        except Exception:
            pass

        try:
            page.remove_listener(
                "request",
                on_request,
            )
        except Exception:
            pass

    # Prefer endpoints matching the current section.
    section_tokens = {
        "external": (
            "/external/advertising/products/",
        ),
        "goods": (
            "/advertising/products/",
        ),
        "brands": (
            "/brand",
            "/brands",
        ),
        "bonus_purchase": (
            "/bonuses/products/",
        ),
        "bonus_review": (
            "/bonuses/reviews/",
        ),
    }.get(
        section_key,
        (),
    )

    captured.sort(
        key=lambda entry: (
            0
            if any(
                token
                in entry[
                    "url"
                ].lower()
                for token in section_tokens
            )
            else 1,
            -len(
                entry[
                    "records"
                ]
            ),
        )
    )

    result = []
    seen = set()

    for entry in captured:
        for record in entry[
            "records"
        ]:
            marker = (
                record[
                    "campaign_id"
                ],
                record[
                    "name"
                ].lower(),
            )

            if marker in seen:
                continue

            seen.add(marker)
            result.append(
                record
            )

    # Detailed but compact diagnostics when no campaigns were extracted.
    if not result:
        print()
        print(
            "=== MARKETING ENDPOINT PROBE ==="
        )
        print(
            "section:",
            section_key,
        )
        print(
            "page:",
            page.url,
        )

        print(
            "Requests observed:",
            len(
                request_urls
            ),
        )

        for index, url in enumerate(
            request_urls[:80],
            start=1,
        ):
            print(
                f"REQ {index:02d}:",
                url,
            )

        useful = sorted(
            diagnostics,
            key=lambda item: (
                0
                if item.get(
                    "shape"
                )
                else 1,
                0
                if any(
                    token
                    in str(
                        item.get(
                            "url"
                        )
                    ).lower()
                    for token in section_tokens
                )
                else 1,
            ),
        )

        print(
            "Responses inspected:",
            len(
                useful
            ),
        )

        for index, item in enumerate(
            useful[:60],
            start=1,
        ):
            print(
                f"RESP {index:02d}:",
                "status=",
                item.get(
                    "status"
                ),
                "records=",
                item.get(
                    "records"
                ),
                "type=",
                item.get(
                    "content_type"
                ),
            )
            print(
                "   URL:",
                item.get(
                    "url"
                ),
            )

            if item.get(
                "shape"
            ):
                try:
                    print(
                        "   SHAPE:",
                        json.dumps(
                            item[
                                "shape"
                            ],
                            ensure_ascii=False,
                            default=str,
                        )[:1500],
                    )
                except Exception:
                    print(
                        "   SHAPE:",
                        str(
                            item[
                                "shape"
                            ]
                        )[:1500],
                    )

        print(
            "=== END MARKETING ENDPOINT PROBE ==="
        )
        print()

    else:
        print(
            "Marketing campaigns captured from API:",
            section_key,
            "count=",
            len(result),
            "urls=",
            [
                entry[
                    "url"
                ]
                for entry in captured[:5]
            ],
        )

    return result


async def _marketing_campaign_record_from_row(
    row,
    merchant_id,
):
    """
    Parse one campaign row.

    Critical detail: use the link text inside the first column as campaign
    name. Do NOT use the full first-cell text because it can include status
    text such as "Приостановлена".
    """
    try:
        if not await row.is_visible():
            return None
    except Exception:
        return None

    first = None

    # Native table.
    try:
        cells = row.locator("td")
        if await cells.count():
            first = cells.first
    except Exception:
        pass

    # ARIA table/grid.
    if first is None:
        try:
            cells = row.locator(
                '[role="cell"], [role="gridcell"]'
            )
            if await cells.count():
                first = cells.first
        except Exception:
            pass

    if first is None:
        return None

    name = ""
    href = ""

    # Prefer the real clickable campaign title.
    try:
        link = first.locator("a").first
        if (
            await link.count()
            and await link.is_visible()
        ):
            name = " ".join(
                (
                    await link.inner_text()
                ).split()
            )
            href = (
                await link.get_attribute(
                    "href"
                )
                or ""
            )
    except Exception:
        pass

    # Some SPA tables use a role/button instead of <a>.
    if not name:
        try:
            clickable = first.locator(
                '[role="button"], button'
            ).first
            if (
                await clickable.count()
                and await clickable.is_visible()
            ):
                name = " ".join(
                    (
                        await clickable.inner_text()
                    ).split()
                )
        except Exception:
            pass

    # Last resort: first line only, never all status text.
    if not name:
        try:
            raw = await first.inner_text()
            lines = [
                " ".join(line.split())
                for line in str(raw).splitlines()
                if " ".join(line.split())
            ]
            if lines:
                name = lines[0]
        except Exception:
            pass

    name = " ".join(
        str(name or "").split()
    )

    if (
        not name
        or len(name) > 180
    ):
        return None

    low = name.lower()

    if low in {
        "название",
        "обзор",
        "мои кампании",
        "создать кампанию",
        "скачать отчет",
        "скачать отчёт",
        "редактировать",
        "действия",
        "все",
        "приостановленные",
        "удаленные",
        "удалённые",
    }:
        return None

    # Metrics-only cells are not campaign names.
    if re.fullmatch(
        r"[0-9\s₸%().,/\-]+",
        name,
    ):
        return None

    return {
        "name": name,
        "href": href,
        "campaign_id": (
            marketing_extract_campaign_id(
                href
            )
        ),
        "merchant_id": merchant_id,
    }


async def marketing_discover_campaign_records(
    shop_id,
    section_key,
):
    # External advertising and product advertising no longer depend on
    # clicking dashboard cards. We already know the exact Kaspi APIs.
    if section_key in {"external", "goods"}:
        direct_records = await marketing_direct_campaign_records(
            shop_id,
            section_key,
        )

        # [] is intentional: there may simply be zero active campaigns.
        # Only None means direct API failed and UI fallback is justified.
        if direct_records is not None:
            return direct_records

    # Fallback UI/network discovery remains only for a real API failure
    # and for bonus sections.
    context, page = await marketing_open_campaigns(
        shop_id,
        section_key,
    )

    if not context:
        return []

    try:
        merchant_id = (
            await marketing_discover_merchant_id_from_page(
                page
            )
        )

        records = []
        seen = set()

        # 1. Real HTML table rows.
        native_rows = page.locator(
            "table tbody tr"
        )

        for i in range(
            min(
                await native_rows.count(),
                1000,
            )
        ):
            record = (
                await _marketing_campaign_record_from_row(
                    native_rows.nth(i),
                    merchant_id,
                )
            )

            if not record:
                continue

            key = record[
                "name"
            ].lower()

            if key in seen:
                continue

            seen.add(key)
            records.append(
                record
            )

        # 2. ARIA grid/table rows.
        if not records:
            aria_rows = page.locator(
                '[role="table"] [role="row"], '
                '[role="grid"] [role="row"]'
            )

            for i in range(
                min(
                    await aria_rows.count(),
                    1000,
                )
            ):
                record = (
                    await _marketing_campaign_record_from_row(
                        aria_rows.nth(i),
                        merchant_id,
                    )
                )

                if not record:
                    continue

                key = record[
                    "name"
                ].lower()

                if key in seen:
                    continue

                seen.add(key)
                records.append(
                    record
                )

        # Vue can render campaign lists as divs rather than <table>.
        # Capture the JSON used by the SPA for ads AND both bonus sections.
        if (
            section_key
            in {
                "external",
                "goods",
                "bonus_purchase",
                "bonus_review",
            }
            and not records
        ):
            records = await _marketing_campaigns_from_network(
                page,
                section_key,
            )

        # For advertising sections we NEVER scan arbitrary navigation text.
        if (
            section_key
            in {
                "external",
                "goods",
            }
            and not records
        ):
            await _marketing_screen(
                shop_id,
                page,
            )
            print(
                "Marketing campaigns not recognized from DOM or API:",
                section_key,
                "url=",
                page.url,
            )
            return []

        # Non-advertising sections can have different campaign/card UIs.
        # Keep a conservative fallback there only.
        if not records:
            candidates = page.locator(
                'main a, main [role="button"], '
                '[data-testid*="campaign" i]'
            )

            ignore = {
                "обзор",
                "мои кампании",
                "создать кампанию",
                "скачать отчет",
                "скачать отчёт",
                "редактировать",
                "действия",
                "все",
                "приостановленные",
                "удаленные",
                "удалённые",
                "реклама",
                "реклама для брендов",
                "внешняя реклама",
                "реклама товаров в магазине",
                "акции с бонусами",
                "акции kaspi.kz",
                "kaspi гид",
            }

            for i in range(
                min(
                    await candidates.count(),
                    500,
                )
            ):
                loc = candidates.nth(
                    i
                )

                try:
                    if not await loc.is_visible():
                        continue

                    name = " ".join(
                        (
                            await loc.inner_text()
                        ).split()
                    )
                    low = name.lower()

                    if (
                        not name
                        or len(name) > 160
                        or low in ignore
                    ):
                        continue

                    href = (
                        await loc.get_attribute(
                            "href"
                        )
                        or ""
                    )

                    campaign_id = (
                        marketing_extract_campaign_id(
                            href
                        )
                    )

                    # Conservative fallback: require a real campaign id/href.
                    if not campaign_id:
                        continue

                    if low in seen:
                        continue

                    seen.add(low)
                    records.append({
                        "name": name,
                        "href": href,
                        "campaign_id": campaign_id,
                        "merchant_id": merchant_id,
                    })

                except Exception:
                    pass

        return records[:500]

    finally:
        await context.close()


async def marketing_discover_campaigns(
    shop_id,
    section_key,
):
    return [
        record["name"]
        for record in (
            await marketing_discover_campaign_records(
                shop_id,
                section_key,
            )
        )
    ]


async def marketing_try_set_full_period(page):
    triggers = [
        'button:has-text("Период")',
        '[role="button"]:has-text("Период")',
        'button:has-text("Дата")',
        '[role="button"]:has-text("Дата")',
        '[aria-label*="период" i]',
        '[aria-label*="дата" i]',
    ]
    trigger = await _first_visible(page, triggers)
    if not trigger:
        return False

    try:
        await trigger.click()
        await page.wait_for_timeout(350)
    except Exception:
        return False

    for option in (
        "За всё время",
        "За все время",
        "Весь период",
        "Все время",
        "Всё время",
        "Максимальный период",
    ):
        try:
            loc = page.get_by_text(option, exact=True).first
            if await loc.count() and await loc.is_visible():
                await loc.click()
                await page.wait_for_timeout(700)
                return True
        except Exception:
            pass

    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    return False


async def marketing_download_campaign_report(
    shop_id,
    section_key,
    campaign,
):
    campaign_name = (
        campaign.get("name")
        if isinstance(campaign, dict)
        else str(campaign)
    )
    context, page = await marketing_open_campaigns(
        shop_id,
        section_key,
    )

    if not context:
        return None, "Marketing session is not connected"

    try:
        campaign_clicked = False

        # Native table: click only the title link in the first cell of
        # the row whose title matches exactly.
        rows = page.locator(
            "table tbody tr"
        )

        for i in range(
            min(
                await rows.count(),
                1000,
            )
        ):
            row = rows.nth(i)

            try:
                if not await row.is_visible():
                    continue

                cells = row.locator(
                    "td"
                )
                if not await cells.count():
                    continue

                first = cells.first
                link = first.locator(
                    "a"
                ).first

                if (
                    await link.count()
                    and await link.is_visible()
                ):
                    link_name = " ".join(
                        (
                            await link.inner_text()
                        ).split()
                    )

                    if link_name == campaign_name:
                        await link.click()
                        campaign_clicked = True
                        break

                # SPA fallback: first line/clickable in first cell.
                clickable = first.locator(
                    '[role="button"], button'
                ).first

                if (
                    not campaign_clicked
                    and await clickable.count()
                    and await clickable.is_visible()
                ):
                    click_name = " ".join(
                        (
                            await clickable.inner_text()
                        ).split()
                    )

                    if click_name == campaign_name:
                        await clickable.click()
                        campaign_clicked = True
                        break

            except Exception:
                pass

        # ARIA grid fallback.
        if not campaign_clicked:
            aria_rows = page.locator(
                '[role="table"] [role="row"], '
                '[role="grid"] [role="row"]'
            )

            for i in range(
                min(
                    await aria_rows.count(),
                    1000,
                )
            ):
                row = aria_rows.nth(i)

                try:
                    cells = row.locator(
                        '[role="cell"], [role="gridcell"]'
                    )
                    if not await cells.count():
                        continue

                    first = cells.first
                    link = first.locator(
                        "a"
                    ).first

                    if (
                        await link.count()
                        and await link.is_visible()
                    ):
                        link_name = " ".join(
                            (
                                await link.inner_text()
                            ).split()
                        )

                        if link_name == campaign_name:
                            await link.click()
                            campaign_clicked = True
                            break
                except Exception:
                    pass

        if not campaign_clicked:
            await _marketing_screen(
                shop_id,
                page,
            )
            return (
                None,
                f"Не нашёл кампанию «{campaign_name}» "
                "в колонке «Название».",
            )

        try:
            await page.wait_for_load_state(
                "domcontentloaded",
                timeout=12000,
            )
        except Exception:
            pass

        await page.wait_for_timeout(
            1000
        )

        await marketing_try_set_full_period(page)

        download_candidates = [
            'button:has-text("Скачать")',
            'a:has-text("Скачать")',
            '[aria-label*="скач" i]',
            '[title*="скач" i]',
            '[data-testid*="download" i]',
            'a[download]',
        ]

        button = await _first_visible(
            page,
            download_candidates,
        )

        if not button:
            await _marketing_screen(
                shop_id,
                page,
            )
            return (
                None,
                "На странице кампании не найдена кнопка скачивания отчёта.",
            )

        async with page.expect_download(
            timeout=30000
        ) as info:
            await button.click()

        download = await info.value
        suggested = (
            download.suggested_filename
            or (
                "marketing_report.csv"
                if section_key in {
                    "goods",
                    "bonus_purchase",
                }
                else "marketing_report.xlsx"
            )
        )
        source_url = download.url

        temp_path = (
            MARKETING_DIR
            / f"download_{shop_id}_{secrets.token_hex(6)}"
        )
        await download.save_as(
            str(temp_path)
        )

        try:
            payload = temp_path.read_bytes()
        finally:
            try:
                temp_path.unlink()
            except Exception:
                pass

        return (
            suggested,
            payload,
            source_url,
        ), None

    except Exception as exc:
        try:
            await _marketing_screen(
                shop_id,
                page,
            )
        except Exception:
            pass
        return None, str(exc)

    finally:
        await context.close()


async def marketing_scrape_visible_tables(
    shop_id,
    section_key,
):
    context, page = await marketing_open_campaigns(
        shop_id,
        section_key,
    )

    if not context:
        return []

    try:
        rows = []
        tables = page.locator("table")

        for table_index in range(
            await tables.count()
        ):
            table = tables.nth(table_index)
            tr = table.locator("tr")
            matrix = []

            for i in range(await tr.count()):
                cells = tr.nth(i).locator("th,td")
                values = []

                for j in range(await cells.count()):
                    try:
                        values.append(
                            " ".join(
                                (
                                    await cells.nth(j).inner_text()
                                ).split()
                            )
                        )
                    except Exception:
                        values.append("")

                if any(values):
                    matrix.append(values)

            if len(matrix) < 2:
                continue

            headers = matrix[0]
            for values in matrix[1:]:
                row = {}
                for index, header in enumerate(headers):
                    key = header or f"column_{index+1}"
                    row[key] = (
                        values[index]
                        if index < len(values)
                        else ""
                    )
                if row:
                    rows.append(row)

        await _marketing_screen(
            shop_id,
            page,
        )
        return rows

    finally:
        await context.close()


MARKETING_COLUMN_ALIASES = {
    "date": (
        "дата", "день", "date", "дата расхода",
        "дата списания", "дата заказа",
    ),
    "product_code": (
        "код товара", "kaspi код", "код kaspi",
        "product code", "id товара", "товар id",
    ),
    "sku": (
        "sku", "артикул", "merchant sku", "код продавца",
    ),
    "product_name": (
        "товар", "название товара", "наименование", "product",
    ),
    "order_code": (
        "номер заказа", "заказ", "order", "order id",
        "номер рекламного заказа",
    ),
    "amount": (
        "стоимость", "расходы", "расход", "затраты", "потрачено",
        "списано", "сумма списания", "сумма бонусов", "бонусы",
        "начислено бонусов", "amount", "cost", "spend",
    ),
    "clicks": (
        "клики", "переходы", "clicks",
    ),
    "impressions": (
        "показы", "просмотры", "impressions", "views",
    ),
    "orders_count": (
        "количество заказов", "заказы", "все заказы", "orders",
    ),
    "revenue": (
        "сумма заказов", "выручка", "доход", "revenue", "sales",
    ),
}


def _marketing_norm_header(value):
    value = " ".join(
        str(value or "").strip().lower().split()
    )
    return value.replace("ё", "е")


def _marketing_find_column(headers, logical):
    aliases = tuple(
        _marketing_norm_header(x)
        for x in MARKETING_COLUMN_ALIASES[logical]
    )
    normalized = [
        _marketing_norm_header(header)
        for header in headers
    ]

    for index, header in enumerate(normalized):
        if header in aliases:
            return index

    for index, header in enumerate(normalized):
        for alias in aliases:
            if alias and len(alias) >= 4 and alias in header:
                return index

    return None


def _marketing_number(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    raw = str(value).strip()
    if not raw:
        return None

    raw = (
        raw.replace("\\xa0", "")
        .replace(" ", "")
        .replace("₸", "")
        .replace("тг", "")
        .replace("%", "")
        .replace(",", ".")
    )
    raw = re.sub(r"[^0-9.\\-]", "", raw)

    if raw in {"", "-", ".", "-."}:
        return None

    try:
        return float(raw)
    except Exception:
        return None


def _marketing_business_date_from_datetime(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    else:
        value = value.astimezone(TZ)

    result = value.date()

    if value.hour < BUSINESS_DAY_START_HOUR:
        result -= timedelta(days=1)

    return result.isoformat()


def _marketing_report_date(value):
    """
    Marketing event timestamps use the same business day as orders:
    11:00 Asia/Almaty -> 11:00 the next calendar day.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        return _marketing_business_date_from_datetime(
            value
        )

    if isinstance(value, date):
        return value.isoformat()

    raw = str(value).strip()

    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )

        if (
            "T" in raw
            or re.search(
                r"\d{1,2}:\d{2}",
                raw,
            )
        ):
            return _marketing_business_date_from_datetime(
                parsed
            )

        return parsed.date().isoformat()
    except Exception:
        pass

    for fmt in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
    ):
        try:
            return _marketing_business_date_from_datetime(
                datetime.strptime(
                    raw,
                    fmt,
                )
            )
        except Exception:
            pass

    match = re.search(
        r"(\d{1,2})[.\-/](\d{1,2})[.\-/](20\d{2})"
        r"\s+(\d{1,2}):(\d{2})(?::(\d{2}))?",
        raw,
    )

    if match:
        try:
            return _marketing_business_date_from_datetime(
                datetime(
                    int(match.group(3)),
                    int(match.group(2)),
                    int(match.group(1)),
                    int(match.group(4)),
                    int(match.group(5)),
                    int(match.group(6) or 0),
                    tzinfo=TZ,
                )
            )
        except Exception:
            pass

    for fmt in (
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(
                raw,
                fmt,
            ).date().isoformat()
        except Exception:
            pass

    return None


def _marketing_normalize_matrix_rows(matrix):
    if not matrix:
        return [], []

    best_index = 0
    best_score = -1
    alias_words = set()

    for values in MARKETING_COLUMN_ALIASES.values():
        alias_words.update(
            _marketing_norm_header(x)
            for x in values
        )

    for i, row in enumerate(matrix[:25]):
        score = 0

        for value in row:
            header = _marketing_norm_header(value)
            if not header:
                continue

            if header in alias_words:
                score += 3
            elif any(
                alias in header
                for alias in alias_words
                if len(alias) >= 4
            ):
                score += 1

        if score > best_score:
            best_score = score
            best_index = i

    headers = [
        str(value or "").strip()
        for value in matrix[best_index]
    ]
    rows = matrix[best_index + 1:]
    return headers, rows


def _marketing_raw_row_is_total(raw_row):
    for value in raw_row.values():
        normalized = _marketing_norm_header(
            value
        )
        if normalized in {
            "итого",
            "всего",
            "total",
            "grand total",
        }:
            return True
    return False


def _marketing_clean_order_code(value):
    if value in (None, ""):
        return None

    raw = str(value).strip()

    if not raw:
        return None

    # Kaspi report exports can use 0 / 0.0 as "no order".
    if raw.lower() in {
        "0",
        "0.0",
        "0,0",
        "none",
        "null",
        "-",
        "—",
    }:
        return None

    return raw


def marketing_parse_xlsx(payload, fallback_date=None):
    workbook = load_workbook(
        io.BytesIO(payload),
        data_only=True,
        read_only=True,
    )

    parsed = []

    for sheet in workbook.worksheets:
        matrix = []

        for row in sheet.iter_rows(values_only=True):
            values = list(row)
            if any(
                value not in (None, "")
                for value in values
            ):
                matrix.append(values)

        headers, rows = _marketing_normalize_matrix_rows(
            matrix
        )

        if not headers:
            continue

        col = {
            key: _marketing_find_column(headers, key)
            for key in MARKETING_COLUMN_ALIASES
        }

        for values in rows:
            if not any(
                value not in (None, "")
                for value in values
            ):
                continue

            raw_row = {}
            for index, header in enumerate(headers):
                key = header or f"column_{index+1}"
                raw_row[key] = (
                    values[index]
                    if index < len(values)
                    else None
                )

            if _marketing_raw_row_is_total(
                raw_row
            ):
                continue

            def cell(logical):
                index = col.get(logical)
                if index is None or index >= len(values):
                    return None
                return values[index]

            parsed.append({
                "sheet": sheet.title,
                "date": _marketing_report_date(cell("date")) or fallback_date,
                "product_code": (
                    str(cell("product_code")).strip()
                    if cell("product_code") not in (None, "")
                    else None
                ),
                "sku": (
                    str(cell("sku")).strip()
                    if cell("sku") not in (None, "")
                    else None
                ),
                "product_name": (
                    str(cell("product_name")).strip()
                    if cell("product_name") not in (None, "")
                    else None
                ),
                "order_code": _marketing_clean_order_code(
                    cell("order_code")
                ),
                "amount": _marketing_number(cell("amount")) or 0.0,
                "clicks": _marketing_number(cell("clicks")),
                "impressions": _marketing_number(cell("impressions")),
                "orders_count": _marketing_number(cell("orders_count")),
                "revenue": _marketing_number(cell("revenue")),
                "raw": raw_row,
            })

    return parsed


def marketing_parse_csv(
    payload,
    fallback_date=None,
):
    decoded = None

    for encoding in (
        "utf-8-sig",
        "utf-8",
        "cp1251",
    ):
        try:
            decoded = payload.decode(
                encoding
            )
            break
        except Exception:
            pass

    if decoded is None:
        decoded = payload.decode(
            "utf-8",
            errors="replace",
        )

    sample = decoded[:10000]

    try:
        dialect = csv.Sniffer().sniff(
            sample,
            delimiters=";,\\t,",
        )
        delimiter = dialect.delimiter
    except Exception:
        # Kaspi CSV exports commonly use semicolon.
        delimiter = ";"

    matrix = list(
        csv.reader(
            io.StringIO(decoded),
            delimiter=delimiter,
        )
    )

    matrix = [
        row
        for row in matrix
        if any(
            str(value).strip()
            for value in row
        )
    ]

    headers, rows = (
        _marketing_normalize_matrix_rows(
            matrix
        )
    )

    if not headers:
        return []

    col = {
        key: _marketing_find_column(
            headers,
            key,
        )
        for key in MARKETING_COLUMN_ALIASES
    }

    parsed = []

    for values in rows:
        raw_row = {}
        for index, header in enumerate(
            headers
        ):
            raw_row[
                header
                or f"column_{index+1}"
            ] = (
                values[index]
                if index < len(values)
                else None
            )

        if _marketing_raw_row_is_total(
            raw_row
        ):
            continue

        def cell(logical):
            index = col.get(
                logical
            )
            if (
                index is None
                or index >= len(values)
            ):
                return None
            return values[index]

        parsed.append({
            "sheet": "csv",
            "date": (
                _marketing_report_date(
                    cell("date")
                )
                or fallback_date
            ),
            "product_code": (
                str(
                    cell(
                        "product_code"
                    )
                ).strip()
                if cell(
                    "product_code"
                ) not in (
                    None,
                    "",
                )
                else None
            ),
            "sku": (
                str(
                    cell(
                        "sku"
                    )
                ).strip()
                if cell(
                    "sku"
                ) not in (
                    None,
                    "",
                )
                else None
            ),
            "product_name": (
                str(
                    cell(
                        "product_name"
                    )
                ).strip()
                if cell(
                    "product_name"
                ) not in (
                    None,
                    "",
                )
                else None
            ),
            "order_code": _marketing_clean_order_code(
                cell(
                    "order_code"
                )
            ),
            "amount": (
                _marketing_number(
                    cell(
                        "amount"
                    )
                )
                or 0.0
            ),
            "clicks": _marketing_number(
                cell(
                    "clicks"
                )
            ),
            "impressions": _marketing_number(
                cell(
                    "impressions"
                )
            ),
            "orders_count": _marketing_number(
                cell(
                    "orders_count"
                )
            ),
            "revenue": _marketing_number(
                cell(
                    "revenue"
                )
            ),
            "raw": raw_row,
        })

    return parsed


def marketing_parse_scraped_rows(rows):
    parsed = []

    for raw_row in rows:
        if _marketing_raw_row_is_total(
            raw_row
        ):
            continue

        headers = list(raw_row.keys())
        col_name = {}

        for logical in MARKETING_COLUMN_ALIASES:
            index = _marketing_find_column(headers, logical)
            col_name[logical] = (
                headers[index]
                if index is not None
                else None
            )

        def value(logical):
            key = col_name.get(logical)
            return raw_row.get(key) if key else None

        parsed.append({
            "sheet": "web",
            "date": _marketing_report_date(value("date")),
            "product_code": (
                str(value("product_code")).strip()
                if value("product_code") else None
            ),
            "sku": (
                str(value("sku")).strip()
                if value("sku") else None
            ),
            "product_name": (
                str(value("product_name")).strip()
                if value("product_name") else None
            ),
            "order_code": _marketing_clean_order_code(
                value("order_code")
            ),
            "amount": _marketing_number(value("amount")) or 0.0,
            "clicks": _marketing_number(value("clicks")),
            "impressions": _marketing_number(value("impressions")),
            "orders_count": _marketing_number(value("orders_count")),
            "revenue": _marketing_number(value("revenue")),
            "raw": raw_row,
        })

    return parsed


def marketing_store_parsed_rows(
    shop_id,
    cabinet_key,
    cabinet_name,
    section_key,
    section_name,
    campaign_name,
    source_file,
    rows,
    sync_run_id,
):
    imported = 0

    for row in rows:
        # Profit accounting needs actual Marketing expense. Product rows with
        # 0 ₸ spend are useful only as raw ad statistics and must not clutter
        # the profit DB or detailed reports.
        amount = float(
            row.get("amount")
            or 0
        )

        if abs(amount) < 0.000001:
            continue

        row["order_code"] = _marketing_clean_order_code(
            row.get("order_code")
        )

        # Daily accounting requires a real report date. Broad Kaspi exports
        # often contain no date column; storing those rows caused misleading
        # aggregates and later cleanups could erase valid historical days.
        if not row.get("date"):
            continue

        mapping = DB.resolve_marketing_product(
            shop_id,
            product_code=row.get("product_code"),
            sku=row.get("sku"),
            product_name=row.get("product_name"),
        )

        product_code = (
            mapping.get("product_code")
            or row.get("product_code")
        )
        sku = (
            mapping.get("sku")
            or row.get("sku")
        )
        product_name = (
            mapping.get("name")
            or row.get("product_name")
        )

        fingerprint_payload = {
            "cabinet_key": cabinet_key,
            "section_key": section_key,
            "campaign_name": campaign_name,
            "date": row.get("date"),
            "product_code": product_code,
            "sku": sku,
            "product_name": product_name,
            "order_code": row.get("order_code"),
            "amount": amount,
            "clicks": row.get("clicks"),
            "impressions": row.get("impressions"),
            "orders_count": row.get("orders_count"),
            "revenue": row.get("revenue"),
        }

        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()

        DB.upsert_marketing_report_row(
            shop_id=shop_id,
            cabinet_key=cabinet_key,
            cabinet_name=cabinet_name,
            section_key=section_key,
            section_name=section_name,
            campaign_name=campaign_name,
            report_date=row.get("date"),
            product_code=product_code,
            sku=sku,
            product_name=product_name,
            order_code=row.get("order_code"),
            amount=row.get("amount"),
            clicks=row.get("clicks"),
            impressions=row.get("impressions"),
            orders_count=row.get("orders_count"),
            revenue=row.get("revenue"),
            mapping_quality=mapping.get("quality"),
            source_file=source_file,
            fingerprint=fingerprint,
            sync_run_id=sync_run_id,
            raw_json=json.dumps(
                row.get("raw") or {},
                ensure_ascii=False,
                default=str,
            ),
        )
        imported += 1

    return imported


async def marketing_ensure_session(shop_id):
    """
    Reuse saved browser session first.
    If expired, silently reuse encrypted saved credentials.
    Only OTP/confirmation requires user action.
    """
    state, error = await marketing_check_session(shop_id)

    if state == "connected":
        return state, error

    if state == "login":
        credentials = DB.marketing_credentials(shop_id)

        if not credentials:
            return "login_required", "Нет сохранённого логина/пароля."

        state, error = await marketing_login_direct(
            shop_id,
            credentials[0],
            credentials[1],
        )

        if DB.marketing_account(shop_id):
            DB.update_marketing_status(
                shop_id,
                state,
                error,
            )

    return state, error


async def marketing_check_session(shop_id):
    context = await _marketing_context(shop_id)
    page = await context.new_page()
    try:
        await page.goto(
            KASPI_MARKETING_SIGN_IN_URL,
            wait_until="domcontentloaded",
            timeout=MARKETING_BROWSER_TIMEOUT_MS,
        )
        await page.wait_for_timeout(1500)
        state = await _marketing_state(page)
        await _marketing_screen(shop_id, page)
        if state == "connected":
            await _marketing_save_storage(shop_id, context)
        return state, None
    except Exception as exc:
        return "error", str(exc)
    finally:
        await context.close()


async def send_marketing_screen(context, chat_id, shop_id, caption):
    path = marketing_screen_file(shop_id)
    if path.exists():
        try:
            with path.open("rb") as fh:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=fh,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                return
        except Exception:
            pass
    await context.bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode=ParseMode.HTML,
    )


def marketing_parse_source_url(
    section_key,
    url,
):
    return {
        "merchant_id": (
            marketing_extract_merchant_id(
                url
            )
        ),
        "campaign_id": (
            marketing_extract_campaign_id(
                url
            )
        ),
        "file_kind": (
            "csv"
            if "/csv" in str(url)
            else (
                "xlsx"
                if "/xlsx" in str(url)
                else marketing_report_file_kind(
                    section_key
                )
            )
        ),
    }


def marketing_sync_window(
    shop_id,
):
    today = current_business_date()
    last = DB.last_marketing_sync(
        shop_id
    )

    days = (
        MARKETING_REFRESH_DAYS
        if last and last["finished_at"]
        else MARKETING_INITIAL_LOOKBACK_DAYS
    )

    start = (
        today
        - timedelta(
            days=max(
                0,
                days - 1,
            )
        )
    )

    return start, today


async def marketing_direct_or_ui_report(
    shop_id,
    section_key,
    campaign,
    start_date,
    end_date,
    account,
    request_context=None,
):
    campaign_name = campaign[
        "name"
    ]

    cached = DB.marketing_campaign_source(
        shop_id,
        section_key,
        campaign_name,
    )

    merchant_id = (
        campaign.get(
            "merchant_id"
        )
        or (
            cached["merchant_id"]
            if cached
            else None
        )
        or account[
            "selected_merchant_id"
        ]
    )

    campaign_id = (
        campaign.get(
            "campaign_id"
        )
        or (
            cached["campaign_id"]
            if cached
            else None
        )
    )

    direct_url = marketing_direct_report_url(
        section_key,
        merchant_id,
        campaign_id,
        start_date,
        end_date,
    )

    # For four known sections: direct authenticated report download.
    if direct_url:
        result, error = await marketing_request_report(
            shop_id,
            direct_url,
            context=request_context,
        )

        if result:
            return result, None

    # If we have an old captured URL, rebuild dates from its IDs.
    if cached and cached["report_url"]:
        parsed = marketing_parse_source_url(
            section_key,
            cached["report_url"],
        )

        direct_url = marketing_direct_report_url(
            section_key,
            (
                parsed["merchant_id"]
                or merchant_id
            ),
            (
                parsed["campaign_id"]
                or campaign_id
            ),
            start_date,
            end_date,
        )

        if direct_url:
            result, error = await marketing_request_report(
                shop_id,
                direct_url,
            )
            if result:
                return result, None

    # First discovery or unsupported section: use the website button once.
    return await marketing_download_campaign_report(
        shop_id,
        section_key,
        campaign,
    )


async def marketing_fetch_campaign_bundle(
    shop_id,
    section_key,
    campaign_index,
    campaign,
    sync_start_date,
    sync_end_date,
    account,
    semaphore,
):
    """
    Fetch one campaign under bounded concurrency.

    One BrowserContext is reused for:
      - the period report
      - today's report
      - yesterday's report

    This avoids creating three separate contexts per campaign.
    """
    async with semaphore:
        context = await _marketing_context(
            shop_id
        )

        try:
            main_result, main_error = (
                await marketing_direct_or_ui_report(
                    shop_id,
                    section_key,
                    campaign,
                    sync_start_date,
                    sync_end_date,
                    account,
                    request_context=context,
                )
            )

            daily_results = []

            if main_result:
                _, _, source_url = main_result

                source_meta = marketing_parse_source_url(
                    section_key,
                    source_url,
                )

                merchant_id = (
                    source_meta.get("merchant_id")
                    or campaign.get("merchant_id")
                    or account["selected_merchant_id"]
                )
                campaign_id = (
                    source_meta.get("campaign_id")
                    or campaign.get("campaign_id")
                )

                if merchant_id and campaign_id:
                    for day_offset in (0, 1):
                        report_day = (
                            current_business_date()
                            - timedelta(days=day_offset)
                        )

                        daily_url = marketing_direct_report_url(
                            section_key,
                            merchant_id,
                            campaign_id,
                            report_day,
                            report_day,
                        )

                        if not daily_url:
                            continue

                        daily_result, daily_error = (
                            await marketing_request_report(
                                shop_id,
                                daily_url,
                                context=context,
                            )
                        )

                        if daily_result:
                            daily_results.append(
                                (
                                    report_day,
                                    daily_result,
                                )
                            )

            return {
                "campaign_index": campaign_index,
                "campaign": campaign,
                "main_result": main_result,
                "main_error": main_error,
                "daily_results": daily_results,
            }

        finally:
            await context.close()


def marketing_persist_campaign_bundle(
    shop_id,
    cabinet_key,
    cabinet_name,
    section_key,
    section_name,
    bundle,
    report_dir,
    run_id,
):
    """
    Persist a fetched campaign bundle synchronously on the event loop.
    Network work is parallel; SQLite/file parsing remains serialized to avoid
    lock contention and keep one-connection SQLite safe.
    """
    campaign_index = bundle["campaign_index"]
    campaign = bundle["campaign"]
    campaign_name = campaign["name"]
    result = bundle["main_result"]

    if not result:
        return {
            "files": 0,
            "rows": 0,
            "had_file": False,
        }

    filename, payload, source_url = result

    source_meta = marketing_parse_source_url(
        section_key,
        source_url,
    )

    merchant_id = (
        source_meta.get("merchant_id")
        or campaign.get("merchant_id")
    )
    campaign_id = (
        source_meta.get("campaign_id")
        or campaign.get("campaign_id")
    )

    if source_meta.get("merchant_id"):
        DB.set_marketing_merchant_id(
            shop_id,
            source_meta["merchant_id"],
        )

    DB.upsert_marketing_campaign_source(
        shop_id=shop_id,
        cabinet_key=cabinet_key,
        cabinet_name=cabinet_name,
        merchant_id=merchant_id,
        section_key=section_key,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        report_url=source_url,
        file_kind=source_meta.get("file_kind"),
    )

    safe_name = re.sub(
        r"[^0-9A-Za-zА-Яа-я._ -]+",
        "_",
        filename,
    )
    saved_path = (
        report_dir
        / (
            f"{section_key}__"
            f"{campaign_index:03d}__"
            f"{safe_name}"
        )
    )
    saved_path.write_bytes(payload)

    rows = []

    try:
        if filename.lower().endswith(
            (".xlsx", ".xlsm", ".xltx", ".xltm")
        ):
            rows = marketing_parse_xlsx(
                payload
            )
        elif filename.lower().endswith(
            ".csv"
        ):
            rows = marketing_parse_csv(
                payload
            )
    except Exception as exc:
        print(
            "Marketing report parse failed:",
            section_key,
            campaign_name,
            repr(exc),
        )
        rows = []

    imported = marketing_store_parsed_rows(
        shop_id,
        cabinet_key,
        cabinet_name,
        section_key,
        section_name,
        campaign_name,
        saved_path.name,
        rows,
        run_id,
    )

    for report_day, daily_result in bundle["daily_results"]:
        daily_filename, daily_payload, _ = daily_result

        try:
            if daily_filename.lower().endswith(
                ".csv"
            ):
                daily_rows = marketing_parse_csv(
                    daily_payload,
                    fallback_date=report_day.isoformat(),
                )
            else:
                daily_rows = marketing_parse_xlsx(
                    daily_payload,
                    fallback_date=report_day.isoformat(),
                )
        except Exception as exc:
            print(
                "Marketing daily parse failed:",
                section_key,
                campaign_name,
                report_day,
                repr(exc),
            )
            continue

        imported += marketing_store_parsed_rows(
            shop_id,
            cabinet_key,
            cabinet_name,
            section_key,
            section_name,
            campaign_name,
            (
                f"daily_{report_day.isoformat()}__"
                f"{daily_filename}"
            ),
            daily_rows,
            run_id,
        )

    return {
        "files": 1,
        "rows": imported,
        "had_file": True,
    }


def _date_sequence(start_date, end_date):
    current = start_date
    result = []

    while current <= end_date:
        result.append(current)
        current += timedelta(days=1)

    return result


def marketing_report_error_is_no_data(error):
    low = str(error or "").lower()

    if not low:
        return False

    return any(
        token in low
        for token in (
            "http 204",
            "http 404",
            "no data",
            "нет данных",
            "данные отсутствуют",
            "report is empty",
            "empty report",
        )
    )


async def marketing_fetch_exact_days_bundle(
    shop_id,
    section_key,
    campaign,
    days,
    account,
    semaphore,
    target_start_date=None,
    target_end_date=None,
):
    async with semaphore:
        context = await _marketing_context(
            shop_id
        )

        results = []

        try:
            merchant_id = (
                campaign.get("merchant_id")
                or account["selected_merchant_id"]
            )
            campaign_id = campaign.get(
                "campaign_id"
            )

            if not merchant_id or not campaign_id:
                return {
                    "campaign": campaign,
                    "results": [],
                    "errors": ["нет merchantId/campaignId"],
                    "no_data": 0,
                    "target_start_date": target_start_date,
                    "target_end_date": target_end_date,
                }

            errors = []
            no_data = 0

            for report_day in days:
                url = marketing_direct_report_url(
                    section_key,
                    merchant_id,
                    campaign_id,
                    report_day,
                    report_day,
                )

                if not url:
                    continue

                result, error = await marketing_request_report(
                    shop_id,
                    url,
                    context=context,
                )

                if result:
                    results.append(
                        (
                            report_day,
                            result,
                        )
                    )
                else:
                    if marketing_report_error_is_no_data(
                        error
                    ):
                        no_data += 1
                    else:
                        errors.append(
                            f"{report_day}: {error or 'не скачан'}"
                        )

            return {
                "campaign": campaign,
                "results": results,
                "errors": errors,
                "no_data": no_data,
                "target_start_date": target_start_date,
                "target_end_date": target_end_date,
            }

        finally:
            await context.close()


def marketing_store_section_total_adjustment(
    shop_id,
    cabinet_key,
    cabinet_name,
    section_key,
    section_name,
    report_date,
    amount,
    sync_run_id,
    source_label,
):
    amount = float(
        amount
        or 0
    )

    campaign_name = "Нераспределённый расход рекламы"

    DB.delete_marketing_report_slice(
        shop_id,
        cabinet_key,
        section_key,
        campaign_name,
        report_date,
    )

    if amount <= 0.000001:
        return 0

    fingerprint_payload = {
        "cabinet_key": cabinet_key,
        "section_key": section_key,
        "campaign_name": campaign_name,
        "date": report_date,
        "amount": round(amount, 6),
        "source": source_label,
    }

    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    DB.upsert_marketing_report_row(
        shop_id=shop_id,
        cabinet_key=cabinet_key,
        cabinet_name=cabinet_name,
        section_key=section_key,
        section_name=section_name,
        campaign_name=campaign_name,
        report_date=report_date,
        product_code=None,
        sku=None,
        product_name=None,
        order_code=None,
        amount=amount,
        clicks=None,
        impressions=None,
        orders_count=None,
        revenue=None,
        mapping_quality="section_total_fallback",
        source_file=source_label,
        fingerprint=fingerprint,
        sync_run_id=sync_run_id,
        raw_json=json.dumps(
            {
                "source": source_label,
                "amount": amount,
                "date": report_date,
            },
            ensure_ascii=False,
        ),
    )
    return 1


def marketing_section_spend_for_day(
    shop_id,
    section_key,
    report_date,
):
    rows = DB.marketing_rows_for_date(
        shop_id,
        report_date,
    )

    return sum(
        float(row["amount"] or 0)
        for row in rows
        if str(row["section_key"] or "") == section_key
    )


def marketing_persist_exact_days_bundle(
    shop_id,
    cabinet_key,
    cabinet_name,
    section_key,
    section_name,
    bundle,
    sync_run_id,
):
    campaign = bundle["campaign"]
    campaign_name = campaign["name"]
    imported = 0
    files = 0

    target_start_date = bundle.get(
        "target_start_date"
    )
    target_end_date = bundle.get(
        "target_end_date"
    )

    if target_start_date and target_end_date:
        for target_day in _date_sequence(
            target_start_date,
            target_end_date,
        ):
            DB.delete_marketing_report_slice(
                shop_id,
                cabinet_key,
                section_key,
                campaign_name,
                target_day.isoformat(),
            )

    for report_day, result in bundle["results"]:
        filename, payload, source_url = result

        source_meta = marketing_parse_source_url(
            section_key,
            source_url,
        )

        merchant_id = (
            source_meta.get("merchant_id")
            or campaign.get("merchant_id")
        )
        campaign_id = (
            source_meta.get("campaign_id")
            or campaign.get("campaign_id")
        )

        DB.upsert_marketing_campaign_source(
            shop_id=shop_id,
            cabinet_key=cabinet_key,
            cabinet_name=cabinet_name,
            merchant_id=merchant_id,
            section_key=section_key,
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            report_url=source_url,
            file_kind=source_meta.get("file_kind"),
        )

        try:
            if filename.lower().endswith(".csv"):
                rows = marketing_parse_csv(
                    payload,
                    fallback_date=report_day.isoformat(),
                )
            else:
                rows = marketing_parse_xlsx(
                    payload,
                    fallback_date=report_day.isoformat(),
                )
        except Exception as exc:
            print(
                "Marketing exact-day parse failed:",
                section_key,
                campaign_name,
                report_day,
                repr(exc),
            )
            continue

        if target_start_date and target_end_date:
            start_s = target_start_date.isoformat()
            end_s = target_end_date.isoformat()
            rows = [
                row
                for row in rows
                if (
                    row.get("date")
                    and start_s
                    <= row["date"]
                    <= end_s
                )
            ]

        imported += marketing_store_parsed_rows(
            shop_id,
            cabinet_key,
            cabinet_name,
            section_key,
            section_name,
            campaign_name,
            (
                f"exact_{report_day.isoformat()}__"
                f"{filename}"
            ),
            rows,
            sync_run_id,
        )
        files += 1

    return files, imported


async def marketing_sync_exact_period(
    shop_id,
    start_date,
    end_date,
    progress=None,
):
    """
    Accurate date accounting.

    Unlike the old broad report sync, this downloads one-day reports for every
    requested date, so Sep 2 / Sep 4 / Sep 5 etc. cannot disappear merely
    because a multi-day export had no date column.
    """
    account = DB.marketing_account(
        shop_id
    )

    if (
        not account
        or not account["selected_cabinet_name"]
    ):
        return {
            "files": 0,
            "rows": 0,
            "sections": [],
            "errors": ["Marketing cabinet not selected"],
        }

    state, error = await marketing_ensure_session(
        shop_id
    )

    if state != "connected":
        return {
            "files": 0,
            "rows": 0,
            "sections": [],
            "errors": [
                "Marketing не авторизован: "
                + str(error or state)
            ],
        }

    await marketing_current_merchant_id(
        shop_id,
        force_refresh=True,
    )
    account = DB.marketing_account(
        shop_id
    )

    cabinet_key = account[
        "selected_cabinet_key"
    ]
    cabinet_name = account[
        "selected_cabinet_name"
    ]

    run_id = DB.start_marketing_sync_run(
        shop_id,
        cabinet_key,
        cabinet_name,
    )

    days = _date_sequence(
        start_date,
        end_date,
    )

    advertising_fetch_days = _date_sequence(
        start_date,
        end_date + timedelta(days=1),
    )

    files_downloaded = 0
    rows_imported = 0
    sections_seen = []
    errors = []

    try:
        for section_index, (
            section_key,
            section_name,
        ) in enumerate(
            MARKETING_REPORT_SECTIONS.items(),
            start=1,
        ):
            if progress:
                await progress(
                    f"📣 {section_index}/{len(MARKETING_REPORT_SECTIONS)} "
                    f"{section_name}: {start_date}–{end_date}"
                )

            if section_key in {
                "external",
                "goods",
            }:
                activity_plan, expected_calendar_cost = (
                    await marketing_ad_activity_plan(
                        shop_id,
                        section_key,
                        start_date,
                        end_date,
                    )
                )

                campaigns = [
                    bucket["campaign"]
                    for bucket in activity_plan.values()
                ]

            elif section_key in {
                "bonus_purchase",
                "bonus_review",
            }:
                campaigns = (
                    await marketing_direct_bonus_campaign_records_for_period(
                        shop_id,
                        section_key,
                        start_date,
                        end_date,
                    )
                )

                if not campaigns:
                    # Confirmed live API fallback: include exact daily
                    # bonusAmount in total Marketing spend even when Kaspi does
                    # not expose campaign rows on the overview page.
                    bonus_files, bonus_rows, bonus_errors = (
                        await marketing_sync_bonus_overview_period(
                            shop_id,
                            cabinet_key,
                            cabinet_name,
                            section_key,
                            section_name,
                            days,
                            run_id,
                        )
                    )

                    files_downloaded += bonus_files
                    rows_imported += bonus_rows
                    errors.extend(
                        f"{section_name}: {item}"
                        for item in bonus_errors[:4]
                    )

                    if bonus_files or bonus_rows:
                        sections_seen.append(
                            section_key
                        )

                    if progress:
                        await progress(
                            f"✅ {section_name}: API overview | "
                            f"дней {bonus_files} | строк {bonus_rows}"
                        )

                    continue

            else:
                campaigns = []

            if not campaigns:
                continue

            semaphore = asyncio.Semaphore(
                MARKETING_SYNC_CONCURRENCY
            )

            tasks = []

            for campaign in campaigns:
                if section_key in MARKETING_ACQUISITION_SECTIONS:
                    marker = (
                        campaign["campaign_id"],
                        campaign["name"].lower(),
                    )
                    campaign_days = sorted(
                        activity_plan[marker]["days"]
                    )
                else:
                    campaign_days = days

                tasks.append(
                    asyncio.create_task(
                        marketing_fetch_exact_days_bundle(
                            shop_id,
                            section_key,
                            campaign,
                            campaign_days,
                            account,
                            semaphore,
                            target_start_date=start_date,
                            target_end_date=end_date,
                        )
                    )
                )

            section_files = 0
            section_rows = 0
            section_no_data = 0
            completed = 0

            for future in asyncio.as_completed(
                tasks
            ):
                try:
                    bundle = await future
                except Exception as exc:
                    errors.append(
                        f"{section_name}: {exc}"
                    )
                    continue

                completed += 1

                section_no_data += int(
                    bundle.get(
                        "no_data",
                        0,
                    )
                    or 0
                )

                if bundle["errors"]:
                    # Keep only a compact diagnostic. One campaign/day failure
                    # must not stop the rest.
                    errors.extend(
                        (
                            f"{section_name} / "
                            f"{bundle['campaign']['name']}: {item}"
                        )
                        for item in bundle["errors"][:2]
                    )

                files, imported = (
                    marketing_persist_exact_days_bundle(
                        shop_id,
                        cabinet_key,
                        cabinet_name,
                        section_key,
                        section_name,
                        bundle,
                        run_id,
                    )
                )

                section_files += files
                section_rows += imported

                if progress:
                    await progress(
                        f"⚡ {section_name}: "
                        f"{completed}/{len(campaigns)} камп. | "
                        f"файлов {section_files} | строк {section_rows}"
                    )

            if section_key in MARKETING_ACQUISITION_SECTIONS:
                for report_day in days:
                    day_s = report_day.isoformat()

                    expected = float(
                        expected_calendar_cost.get(
                            day_s,
                            0,
                        )
                        or 0
                    )

                    current = marketing_section_spend_for_day(
                        shop_id,
                        section_key,
                        day_s,
                    )

                    missing = max(
                        0.0,
                        expected - current,
                    )

                    if missing > 0.01:
                        section_rows += (
                            marketing_store_section_total_adjustment(
                                shop_id,
                                cabinet_key,
                                cabinet_name,
                                section_key,
                                section_name,
                                day_s,
                                missing,
                                run_id,
                                source_label="campaign_list_cost_reconciliation",
                            )
                        )

                if progress:
                    await progress(
                        f"✅ {section_name}: "
                        f"кампаний {len(campaigns)} | "
                        f"файлов {section_files} | "
                        f"строк {section_rows} | "
                        f"без данных {section_no_data}"
                    )

            files_downloaded += section_files
            rows_imported += section_rows

            if section_files or section_rows:
                sections_seen.append(
                    section_key
                )

        DB.finish_marketing_sync_run(
            run_id,
            files_downloaded,
            rows_imported,
            sections_seen,
            error=(
                "; ".join(errors[:10])
                if errors
                else None
            ),
        )

        unique_errors = []

        for item in errors:
            normalized = str(item).strip()
            if normalized and normalized not in unique_errors:
                unique_errors.append(
                    normalized
                )

        return {
            "files": files_downloaded,
            "rows": rows_imported,
            "sections": sections_seen,
            "errors": unique_errors[:20],
        }

    except Exception as exc:
        DB.finish_marketing_sync_run(
            run_id,
            files_downloaded,
            rows_imported,
            sections_seen,
            error=str(exc),
        )
        raise


async def _marketing_period_sync_worker(
    application,
    chat_id,
    shop_id,
    user_id,
    start_date,
    end_date,
    announce=True,
):
    if shop_id in RUNNING_MARKETING_SYNCS:
        if announce:
            await application.bot.send_message(
                chat_id=chat_id,
                text="⏳ Marketing уже синхронизируется в фоне.",
            )
        return

    RUNNING_MARKETING_SYNCS.add(
        shop_id
    )

    status = None

    try:
        if announce:
            status = await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🔄 Обновляю Marketing "
                    f"{start_date}–{end_date}…"
                ),
            )

        async def progress(message):
            if not status:
                return
            try:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status.message_id,
                    text=message,
                )
            except Exception:
                pass

        result = await marketing_sync_exact_period(
            shop_id,
            start_date,
            end_date,
            progress=(
                progress
                if announce
                else None
            ),
        )

        if announce and status:
            title = (
                "✅ Marketing обновлён"
                if not result["errors"]
                else "⚠️ Marketing обновлён частично"
            )
            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status.message_id,
                text=(
                    f"{title}\n\n"
                    f"Период: {start_date}–{end_date}\n"
                    f"Файлов: {result['files']}\n"
                    f"Строк: {result['rows']}\n"
                    f"Ошибок: {len(result['errors'])}"
                    + (
                        "\n\n"
                        + "\n".join(
                            "• " + str(item)[:180]
                            for item in result["errors"][:3]
                        )
                        if result["errors"]
                        else ""
                    )
                ),
            )

    except Exception as exc:
        print(
            "Marketing period sync worker error:",
            repr(exc),
        )
        if announce and status:
            try:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status.message_id,
                    text=(
                        "❌ Marketing не обновлён:\n"
                        + str(exc)[:500]
                    ),
                )
            except Exception:
                pass

    finally:
        RUNNING_MARKETING_SYNCS.discard(
            shop_id
        )


async def marketing_full_sync(
    shop_id,
    progress=None,
):
    account = DB.marketing_account(shop_id)

    invalid_cabinets = {
        "Текущий кабинет Kaspi Marketing",
        "__current__",
        "__disabled_current_fallback__",
    }

    if (
        not account
        or not account["selected_cabinet_name"]
        or str(account["selected_cabinet_name"]) in invalid_cabinets
    ):
        raise RuntimeError(
            "Сначала выберите реальный кабинет Marketing."
        )

    state, error = await marketing_ensure_session(shop_id)

    if state != "connected":
        raise RuntimeError(
            "Marketing не авторизован: "
            + str(error or state)
        )

    cabinet_key = account["selected_cabinet_key"]
    cabinet_name = account["selected_cabinet_name"]

    # Resolve the selected cabinet's current merchant context once before
    # section synchronization. This removes the UI-card dependency for the
    # two advertising sections.
    merchant_id = await marketing_current_merchant_id(
        shop_id,
        force_refresh=True,
    )
    if merchant_id:
        account = DB.marketing_account(
            shop_id
        )

    run_id = DB.start_marketing_sync_run(
        shop_id,
        cabinet_key,
        cabinet_name,
    )

    files_downloaded = 0
    rows_imported = 0
    sections_seen = []
    failed_reports = 0
    sync_warnings = []

    try:
        report_dir = (
            MARKETING_DIR
            / "reports"
            / f"shop_{shop_id}"
        )
        report_dir.mkdir(parents=True, exist_ok=True)

        sync_start_date, sync_end_date = (
            marketing_sync_window(
                shop_id
            )
        )

        for section_index, (
            section_key,
            section_name,
        ) in enumerate(
            MARKETING_REPORT_SECTIONS.items(),
            start=1,
        ):
            if progress:
                await progress(
                    f"📣 {section_index}/{len(MARKETING_REPORT_SECTIONS)} "
                    f"{section_name}"
                )

            campaigns = await marketing_discover_campaign_records(
                shop_id,
                section_key,
            )

            section_opened = bool(campaigns)
            section_had_file = False

            if (
                progress
                and section_key
                in {
                    "external",
                    "goods",
                }
            ):
                if campaigns:
                    active_now = sum(
                        1
                        for campaign in campaigns
                        if campaign.get("sync_reason") == "active"
                    )
                    recent_off = sum(
                        1
                        for campaign in campaigns
                        if campaign.get("sync_reason") == "recent_activity"
                    )
                    await progress(
                        f"✅ {section_name}: активных {active_now}, "
                        f"недавно выключенных с активностью {recent_off}; "
                        "загружаю отчёты"
                    )
                else:
                    await progress(
                        f"ℹ️ {section_name}: нет активных и нет выключенных "
                        f"с активностью за последние {MARKETING_RECENT_ACTIVITY_DAYS} дней"
                    )

            if section_key in {
                "external",
                "goods",
            } and campaigns:
                semaphore = asyncio.Semaphore(
                    MARKETING_SYNC_CONCURRENCY
                )

                # --------------------------------------------------------
                # PRE-FLIGHT:
                # Before launching dozens of campaign requests, prove that
                # real report downloads work for this section.
                #
                # Test up to 2 campaigns concurrently. One success is enough
                # to proceed. If all fail, skip this section immediately.
                # --------------------------------------------------------
                preflight_count = min(
                    MARKETING_PREFLIGHT_CAMPAIGNS,
                    len(campaigns),
                )

                if progress:
                    await progress(
                        f"🧪 {section_name}: проверяю отчёты "
                        f"на {preflight_count} камп."
                    )

                preflight_started = (
                    asyncio.get_running_loop().time()
                )

                preflight_tasks = [
                    asyncio.create_task(
                        marketing_fetch_campaign_bundle(
                            shop_id,
                            section_key,
                            campaign_index,
                            campaign,
                            sync_start_date,
                            sync_end_date,
                            account,
                            semaphore,
                        )
                    )
                    for campaign_index, campaign in enumerate(
                        campaigns[:preflight_count],
                        start=1,
                    )
                ]

                preflight_results = await asyncio.gather(
                    *preflight_tasks,
                    return_exceptions=True,
                )

                preflight_elapsed = (
                    asyncio.get_running_loop().time()
                    - preflight_started
                )

                successful_preflight = []
                failed_preflight_indexes = set()
                preflight_errors = []

                for index, result in enumerate(
                    preflight_results,
                    start=1,
                ):
                    if isinstance(
                        result,
                        Exception,
                    ):
                        failed_preflight_indexes.add(
                            index
                        )
                        preflight_errors.append(
                            f"{index}: {result}"
                        )
                        continue

                    if not result.get(
                        "main_result"
                    ):
                        failed_preflight_indexes.add(
                            index
                        )
                        error_text = (
                            result.get(
                                "main_error"
                            )
                            or "отчёт не скачан"
                        )
                        preflight_errors.append(
                            f"{index}: {error_text}"
                        )
                        continue

                    successful_preflight.append(
                        result
                    )

                if not successful_preflight:
                    failed_reports += preflight_count

                    warning = (
                        f"{section_name}: тест отчётов не прошёл "
                        f"({preflight_count}/{preflight_count}). "
                        "Раздел пропущен, остальные кампании не запускались."
                    )
                    sync_warnings.append(
                        warning
                    )

                    if progress:
                        short_error = (
                            preflight_errors[0][:180]
                            if preflight_errors
                            else "неизвестная ошибка"
                        )
                        await progress(
                            f"❌ {section_name}: тест отчёта не прошёл "
                            f"за {preflight_elapsed:.1f} сек.\n"
                            f"{short_error}\n"
                            "⏭ Остальные кампании этого раздела не запускаю."
                        )

                    # Do NOT waste time on dozens of doomed report requests.
                    continue

                if progress:
                    await progress(
                        f"✅ {section_name}: тест отчёта прошёл "
                        f"за {preflight_elapsed:.1f} сек. "
                        "Запускаю массовую синхронизацию."
                    )

                # Persist successful preflight downloads immediately.
                successful_indexes = set()

                for bundle in successful_preflight:
                    successful_indexes.add(
                        bundle[
                            "campaign_index"
                        ]
                    )

                    persisted = (
                        marketing_persist_campaign_bundle(
                            shop_id,
                            cabinet_key,
                            cabinet_name,
                            section_key,
                            section_name,
                            bundle,
                            report_dir,
                            run_id,
                        )
                    )

                    files_downloaded += persisted[
                        "files"
                    ]
                    rows_imported += persisted[
                        "rows"
                    ]

                    if persisted[
                        "had_file"
                    ]:
                        section_had_file = True

                # Failed preflight campaigns are retried in the normal pool.
                remaining = []

                for campaign_index, campaign in enumerate(
                    campaigns,
                    start=1,
                ):
                    if (
                        campaign_index
                        in successful_indexes
                    ):
                        continue

                    remaining.append(
                        (
                            campaign_index,
                            campaign,
                        )
                    )

                tasks = [
                    asyncio.create_task(
                        marketing_fetch_campaign_bundle(
                            shop_id,
                            section_key,
                            campaign_index,
                            campaign,
                            sync_start_date,
                            sync_end_date,
                            account,
                            semaphore,
                        )
                    )
                    for campaign_index, campaign in remaining
                ]

                completed = len(
                    successful_indexes
                )
                section_failed = 0

                for future in asyncio.as_completed(
                    tasks
                ):
                    try:
                        bundle = await future
                    except Exception as exc:
                        completed += 1
                        section_failed += 1
                        failed_reports += 1

                        if progress:
                            await progress(
                                f"⚠️ {section_name}: "
                                f"{completed}/{len(campaigns)}; "
                                f"ошибок {section_failed}"
                            )
                        continue

                    completed += 1

                    if not bundle.get(
                        "main_result"
                    ):
                        section_failed += 1
                        failed_reports += 1

                    persisted = (
                        marketing_persist_campaign_bundle(
                            shop_id,
                            cabinet_key,
                            cabinet_name,
                            section_key,
                            section_name,
                            bundle,
                            report_dir,
                            run_id,
                        )
                    )

                    files_downloaded += persisted[
                        "files"
                    ]
                    rows_imported += persisted[
                        "rows"
                    ]

                    if persisted[
                        "had_file"
                    ]:
                        section_had_file = True

                    if progress:
                        await progress(
                            f"⚡ {section_name}: "
                            f"{completed}/{len(campaigns)} "
                            f"| файлов {files_downloaded} "
                            f"| ошибок {section_failed} "
                            f"| до {MARKETING_SYNC_CONCURRENCY} параллельно"
                        )

                if section_failed:
                    sync_warnings.append(
                        f"{section_name}: "
                        f"{section_failed} отчётов не загрузилось."
                    )

                # DB account may have received merchant id during persistence.
                account = DB.marketing_account(
                    shop_id
                )

            else:
                for campaign_index, campaign in enumerate(
                    campaigns,
                    start=1,
                ):
                    campaign_name = campaign["name"]

                    if progress:
                        await progress(
                            f"📥 {section_name}: "
                            f"{campaign_index}/{len(campaigns)} "
                            f"{campaign_name[:45]}"
                        )

                    # Persist merchant id as soon as any Marketing resource exposes it.
                    if (
                        campaign.get("merchant_id")
                        and not account["selected_merchant_id"]
                    ):
                        DB.set_marketing_merchant_id(
                            shop_id,
                            campaign["merchant_id"],
                        )
                        account = DB.marketing_account(
                            shop_id
                        )

                    result, download_error = (
                        await marketing_direct_or_ui_report(
                            shop_id,
                            section_key,
                            campaign,
                            sync_start_date,
                            sync_end_date,
                            account,
                        )
                    )

                    if not result:
                        continue

                    filename, payload, source_url = result

                    source_meta = marketing_parse_source_url(
                        section_key,
                        source_url,
                    )

                    if source_meta["merchant_id"]:
                        DB.set_marketing_merchant_id(
                            shop_id,
                            source_meta["merchant_id"],
                        )
                        account = DB.marketing_account(
                            shop_id
                        )

                    DB.upsert_marketing_campaign_source(
                        shop_id=shop_id,
                        cabinet_key=cabinet_key,
                        cabinet_name=cabinet_name,
                        merchant_id=(
                            source_meta["merchant_id"]
                            or campaign.get("merchant_id")
                        ),
                        section_key=section_key,
                        campaign_id=(
                            source_meta["campaign_id"]
                            or campaign.get("campaign_id")
                        ),
                        campaign_name=campaign_name,
                        report_url=source_url,
                        file_kind=source_meta["file_kind"],
                    )
                    section_had_file = True
                    files_downloaded += 1

                    safe_name = re.sub(
                        r"[^0-9A-Za-zА-Яа-я._ -]+",
                        "_",
                        filename,
                    )
                    saved_path = (
                        report_dir
                        / (
                            f"{section_key}__"
                            f"{campaign_index:03d}__"
                            f"{safe_name}"
                        )
                    )
                    saved_path.write_bytes(payload)

                    rows = []

                    try:
                        if filename.lower().endswith(
                            (".xlsx", ".xlsm", ".xltx", ".xltm")
                        ):
                            rows = marketing_parse_xlsx(
                                payload
                            )
                        elif filename.lower().endswith(
                            ".csv"
                        ):
                            rows = marketing_parse_csv(
                                payload
                            )
                    except Exception as exc:
                        print(
                            "Marketing report parse failed:",
                            section_key,
                            campaign_name,
                            repr(exc),
                        )
                        rows = []

                    rows_imported += marketing_store_parsed_rows(
                        shop_id,
                        cabinet_key,
                        cabinet_name,
                        section_key,
                        section_name,
                        campaign_name,
                        saved_path.name,
                        rows,
                        run_id,
                    )

                    # Exact daily accounting for today + yesterday.
                    # If the report itself has no date column, one-day request gives
                    # us a reliable fallback date for profit calculations.
                    cached_now = DB.marketing_campaign_source(
                        shop_id,
                        section_key,
                        campaign_name,
                    )

                    if (
                        cached_now
                        and cached_now["merchant_id"]
                        and cached_now["campaign_id"]
                        and section_key
                        in {
                            "external",
                            "goods",
                            "bonus_purchase",
                            "bonus_review",
                        }
                    ):
                        for day_offset in (
                            0,
                            1,
                        ):
                            report_day = (
                                current_business_date()
                                - timedelta(
                                    days=day_offset
                                )
                            )

                            daily_url = marketing_direct_report_url(
                                section_key,
                                cached_now["merchant_id"],
                                cached_now["campaign_id"],
                                report_day,
                                report_day,
                            )

                            daily_result, daily_error = (
                                await marketing_request_report(
                                    shop_id,
                                    daily_url,
                                )
                            )

                            if not daily_result:
                                continue

                            daily_filename, daily_payload, _ = (
                                daily_result
                            )

                            try:
                                if daily_filename.lower().endswith(
                                    ".csv"
                                ):
                                    daily_rows = marketing_parse_csv(
                                        daily_payload,
                                        fallback_date=report_day.isoformat(),
                                    )
                                else:
                                    daily_rows = marketing_parse_xlsx(
                                        daily_payload,
                                        fallback_date=report_day.isoformat(),
                                    )
                            except Exception as exc:
                                print(
                                    "Marketing daily parse failed:",
                                    section_key,
                                    campaign_name,
                                    report_day,
                                    repr(exc),
                                )
                                continue

                            rows_imported += marketing_store_parsed_rows(
                                shop_id,
                                cabinet_key,
                                cabinet_name,
                                section_key,
                                section_name,
                                campaign_name,
                                (
                                    f"daily_{report_day.isoformat()}__"
                                    f"{daily_filename}"
                                ),
                                daily_rows,
                                run_id,
                            )

            # Bonus and promo sections may expose results without Excel.
            # Also scrape the visible table if no downloadable file was found.
            if (
                not section_had_file
                or section_key in {
                    "bonus_purchase",
                    "bonus_review",
                    "qr",
                    "shop_promotions",
                }
            ):
                try:
                    web_rows = await marketing_scrape_visible_tables(
                        shop_id,
                        section_key,
                    )
                except Exception as exc:
                    print(
                        "Marketing table scrape failed:",
                        section_key,
                        repr(exc),
                    )
                    web_rows = []

                if web_rows:
                    section_opened = True

                parsed_web = marketing_parse_scraped_rows(web_rows)

                rows_imported += marketing_store_parsed_rows(
                    shop_id,
                    cabinet_key,
                    cabinet_name,
                    section_key,
                    section_name,
                    None,
                    f"{section_key}__web",
                    parsed_web,
                    run_id,
                )

            if section_had_file:
                section_opened = True

            if section_opened:
                sections_seen.append(section_key)

        # Historical Marketing rows are intentionally preserved.
        # Exact daily refreshes replace only the campaign/day slice they update.

        DB.finish_marketing_sync_run(
            run_id,
            files_downloaded,
            rows_imported,
            sections_seen,
            error=None,
        )

        return {
            "files": files_downloaded,
            "rows": rows_imported,
            "sections": sections_seen,
            "failed_reports": failed_reports,
            "warnings": sync_warnings,
        }

    except Exception as exc:
        DB.finish_marketing_sync_run(
            run_id,
            files_downloaded,
            rows_imported,
            sections_seen,
            error=str(exc),
        )
        raise


async def _marketing_full_sync_worker_core(
    application,
    chat_id,
    shop_id,
    user_id,
):
    shop = DB.shop(shop_id, user_id)
    if not shop:
        return

    status = await application.bot.send_message(
        chat_id=chat_id,
        text="🔄 Начинаю полную синхронизацию Kaspi Marketing…",
    )

    async def progress(message):
        try:
            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status.message_id,
                text=(
                    "🔄 Синхронизация Marketing\n\n"
                    + message
                ),
            )
        except Exception:
            pass

    try:
        result = await marketing_full_sync(
            shop_id,
            progress=progress,
        )

        recent = DB.marketing_spend_recent_days(
            shop_id,
            limit=7,
        )

        if recent:
            recent_lines = []

            for row in recent:
                report_day = str(
                    row["report_date"]
                )
                section_rows = (
                    DB.marketing_spend_by_section_for_date(
                        shop_id,
                        report_day,
                    )
                )
                section_totals = {
                    key: 0.0
                    for key in MARKETING_BREAKDOWN_ORDER
                }

                for section_row in section_rows:
                    key = str(
                        section_row["section_key"]
                        or ""
                    )
                    if key in section_totals:
                        section_totals[key] += float(
                            section_row["amount"]
                            or 0
                        )

                recent_lines.append(
                    f"• <b>{report_day}</b>"
                )
                recent_lines.extend(
                    marketing_breakdown_html_lines(
                        section_totals
                    )
                )
                recent_lines.append(
                    f"  • Итого: "
                    f"<b>{money(float(row['amount'] or 0))}</b>"
                )
                recent_lines.append("")

            recent_text = "\n".join(
                recent_lines
            ).strip()
        else:
            recent_text = (
                "Пока нет строк с распознанными дневными расходами."
            )

        account = DB.marketing_account(shop_id)

        warnings = result.get(
            "warnings",
            [],
        )
        failed_reports = int(
            result.get(
                "failed_reports",
                0,
            )
            or 0
        )

        if warnings or failed_reports:
            title = (
                "⚠️ <b>Marketing синхронизирован частично</b>"
            )
        else:
            title = (
                "✅ <b>Marketing синхронизирован полностью</b>"
            )

        warning_text = ""

        if warnings:
            warning_text = (
                "\n\n<b>Проблемы:</b>\n"
                + "\n".join(
                    "• "
                    + html.escape(
                        str(item)
                    )
                    for item in warnings[:8]
                )
            )

        await application.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status.message_id,
            text=(
                title
                + "\n\n"
                f"Кабинет: <b>{html.escape(str(account['selected_cabinet_name']))}</b>\n"
                f"Реально открыто разделов: <b>{len(result['sections'])}</b>\n"
                f"Файлов отчётов: <b>{result['files']}</b>\n"
                f"Строк загружено: <b>{result['rows']}</b>\n"
                f"Не загрузилось отчётов: <b>{failed_reports}</b>"
                + warning_text
                + "\n\n<b>Последние расходы:</b>\n"
                + recent_text
            ),
            parse_mode=ParseMode.HTML,
        )

    except Exception as exc:
        await application.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status.message_id,
            text=(
                "❌ Полная синхронизация Marketing не завершена:\n"
                + html.escape(str(exc))
            ),
            parse_mode=ParseMode.HTML,
        )


async def _marketing_full_sync_worker(
    application,
    chat_id,
    shop_id,
    user_id,
):
    if shop_id in RUNNING_MARKETING_SYNCS:
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text="⏳ Синхронизация Marketing этого кабинета уже идёт.",
            )
        except Exception:
            pass
        return

    RUNNING_MARKETING_SYNCS.add(
        shop_id
    )

    try:
        await _marketing_full_sync_worker_core(
            application,
            chat_id,
            shop_id,
            user_id,
        )
    finally:
        RUNNING_MARKETING_SYNCS.discard(
            shop_id
        )


def marketing_status_text(shop, account):
    if not account:
        return (
            f"📣 <b>Маркетинг — {html.escape(shop['name'])}</b>\n\n"
            "Статус: <b>не подключён</b>\n\n"
            "Вход выполняется напрямую браузером в "
            "<code>marketing.kaspi.kz</code>.\n"
            "API, домен и секретный ключ для Marketing не нужны."
        )

    labels = {
        "connected": "✅ подключён",
        "otp": "🔐 нужен код",
        "confirmation": "🔐 нужно подтверждение",
        "login": "⚠️ сессия истекла",
        "unsupported": "⚠️ браузер не поддержан",
        "error": "❌ ошибка",
        "redirected": "❌ ушёл на другую страницу",
    }
    status = str(account["status"] or "unknown")
    lines = [
        f"📣 <b>Маркетинг — {html.escape(shop['name'])}</b>",
        "",
        f"Статус: <b>{labels.get(status, html.escape(status))}</b>",
        "Вход: <code>marketing.kaspi.kz/sign-in</code>",
    ]

    if (
        account["selected_cabinet_name"]
        and str(
            account["selected_cabinet_name"]
        ) not in {
            "Текущий кабинет Kaspi Marketing",
            "__current__",
            "__disabled_current_fallback__",
        }
    ):
        lines.append(
            "Кабинет: <b>"
            + html.escape(
                str(
                    account[
                        "selected_cabinet_name"
                    ]
                )
            )
            + "</b>"
        )

    if account["selected_cabinet_name"]:
        lines.append(
            f"Автообновление: <b>каждые {MARKETING_SYNC_INTERVAL_MINUTES} мин.</b>"
        )

    if account["selected_merchant_id"]:
        lines.append(
            "Marketing Merchant ID: <code>"
            + html.escape(
                str(
                    account[
                        "selected_merchant_id"
                    ]
                )
            )
            + "</code>"
        )

    last_sync = DB.last_marketing_sync(
        shop["id"]
    )
    if last_sync:
        sync_time = (
            last_sync["finished_at"]
            or last_sync["started_at"]
        )
        lines.append(
            "Последняя синхронизация: <b>"
            + html.escape(
                str(sync_time)[:19]
            )
            + "</b>"
        )
    if account["last_error"]:
        lines += ["", f"Ошибка: {html.escape(str(account['last_error']))}"]
    return "\n".join(lines)


def marketing_markup(account=None):
    if account:
        rows = [
            [InlineKeyboardButton("🏢 Выбрать кабинет", callback_data="marketing:cabinets")],
            [InlineKeyboardButton("🔄 Обновить последние 7 дней", callback_data="marketing:syncall")],
            [InlineKeyboardButton("💸 Расходы по дням", callback_data="marketing:spend")],
            [InlineKeyboardButton("📊 Отчёты вручную", callback_data="marketing:reports")],
            [InlineKeyboardButton("🖼 Показать экран Marketing", callback_data="marketing:screen")],
            [InlineKeyboardButton("🔄 Проверить подключение", callback_data="marketing:status")],
            [InlineKeyboardButton("🔐 Войти заново", callback_data="marketing:connect")],
            [InlineKeyboardButton("❌ Отключить", callback_data="marketing:disconnect")],
        ]
    else:
        rows = [[InlineKeyboardButton("🔐 Подключить Marketing", callback_data="marketing:connect")]]

    rows.append([InlineKeyboardButton("🛠 Проверить Chromium и сайт", callback_data="marketing:browsercheck")])
    rows.append([InlineKeyboardButton("⬅️ Закрыть", callback_data="marketing:close")])
    return InlineKeyboardMarkup(rows)


async def cmd_marketing(update, context):
    if not await guard(update):
        return
    shop = await require_shop(update)
    if not shop:
        return

    account = DB.marketing_account(shop["id"])
    await update.effective_message.reply_text(
        marketing_status_text(shop, account),
        parse_mode=ParseMode.HTML,
        reply_markup=marketing_markup(account),
    )


async def callback_marketing(update, context):
    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    shop = DB.active_shop(update.effective_user.id)
    if not shop:
        await query.edit_message_text("Сначала подключите магазин.")
        return

    action = query.data.split(":", 1)[1]

    if action == "close":
        await query.edit_message_text("Маркетинг закрыт.")
        return

    if action == "browsercheck":
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=await marketing_browser_diagnostics(),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "connect":
        context.user_data["shop_flow"] = "marketing_login"
        context.user_data["marketing_shop_id"] = shop["id"]

        await query.edit_message_text(
            "📣 <b>Kaspi Marketing</b>\n\n"
            "Введите <b>номер телефона</b> от "
            "<code>marketing.kaspi.kz</code>.\n"
            "Только 10 цифр — <b>без 8 и без +7</b>.\n"
            "Например: <code>7011234567</code>.\n\n"
            "Данные вводятся один раз: после успешного входа "
            "логин/пароль сохраняются локально в зашифрованном виде, "
            "а браузерная сессия переиспользуется.",
            parse_mode=ParseMode.HTML,
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Номер телефона (10 цифр, без 8 и +7):",
            reply_markup=COST_INPUT_KEYBOARD,
        )
        return

    if action == "screen":
        state, error = await marketing_check_session(
            shop["id"]
        )

        if DB.marketing_account(
            shop["id"]
        ):
            DB.update_marketing_status(
                shop["id"],
                state,
                error,
            )

        await send_marketing_screen(
            context,
            update.effective_chat.id,
            shop["id"],
            (
                f"🖼 Экран Kaspi Marketing\\n"
                f"Статус: {state}"
                + (
                    f"\\nОшибка: {html.escape(str(error))}"
                    if error
                    else ""
                )
            ),
        )
        return

    if action == "cabinets":
        state, session_error = await marketing_ensure_session(
            shop["id"]
        )

        if state != "connected":
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "⚠️ Marketing не авторизован: "
                    + html.escape(
                        str(session_error or state)
                    )
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        cabinets = await marketing_discover_cabinets(
            shop["id"]
        )

        if not cabinets:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "⚠️ Не удалось получить список доступных кабинетов. "
                    "Ничего не выбираю автоматически."
                ),
            )
            await send_marketing_screen(
                context,
                update.effective_chat.id,
                shop["id"],
                "🖼 Экран Kaspi Marketing:",
            )
            return

        context.user_data[
            "marketing_cabinets"
        ] = cabinets

        buttons = []
        for index, cabinet in enumerate(
            cabinets[:50]
        ):
            buttons.append([
                InlineKeyboardButton(
                    (
                        "✅ " + cabinet["name"][:51]
                        if cabinet.get("current")
                        else cabinet["name"][:55]
                    ),
                    callback_data=f"marketcab:{index}",
                )
            ])

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "🏢 <b>Доступные кабинеты Kaspi Marketing</b>\n\n"
                "Выберите нужный кабинет:"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                buttons
            ),
        )
        return

    if action == "syncall":
        account = DB.marketing_account(
            shop["id"]
        )

        if (
            not account
            or not account[
                "selected_cabinet_name"
            ]
        ):
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "Сначала выберите кабинет: "
                    "📣 Маркетинг → 🏢 Выбрать кабинет."
                ),
            )
            return

        today = current_business_date()

        context.application.create_task(
            _marketing_period_sync_worker(
                context.application,
                update.effective_chat.id,
                shop["id"],
                update.effective_user.id,
                today - timedelta(days=6),
                today,
                announce=True,
            ),
            update=update,
            name=f"marketing-7d-sync-{shop['id']}",
        )
        return

    if action == "spend":
        rows = DB.marketing_spend_recent_days(
            shop["id"],
            limit=30,
        )

        if not rows:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "Пока нет синхронизированных расходов Marketing.\n"
                    "Нажмите «🔄 Синхронизировать всё»."
                ),
            )
            return

        lines = [
            "💸 <b>Расходы Marketing по дням</b>",
            "",
        ]

        for row in rows:
            report_day = str(
                row["report_date"]
            )
            section_rows = (
                DB.marketing_spend_by_section_for_date(
                    shop["id"],
                    report_day,
                )
            )
            section_totals = {
                key: 0.0
                for key in MARKETING_BREAKDOWN_ORDER
            }

            for section_row in section_rows:
                key = str(
                    section_row["section_key"]
                    or ""
                )
                if key in section_totals:
                    section_totals[key] += float(
                        section_row["amount"]
                        or 0
                    )

            lines.append(
                f"📅 <b>{report_day}</b>"
            )
            lines.extend(
                marketing_breakdown_html_lines(
                    section_totals
                )
            )
            lines.append(
                f"  • Итого Marketing: "
                f"<b>{money(float(row['amount'] or 0))}</b>"
            )
            lines.append("")

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "reports":
        account = DB.marketing_account(
            shop["id"]
        )
        if not account or not account[
            "selected_cabinet_name"
        ]:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "Сначала выберите кабинет: "
                    "📣 Маркетинг → 🏢 Выбрать кабинет."
                ),
            )
            return

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "📊 <b>Отчёты Marketing</b>\n"
                f"Кабинет: <b>"
                f"{html.escape(str(account['selected_cabinet_name']))}</b>\n\n"
                "Выберите раздел:"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        label,
                        callback_data=f"marketreport:{key}",
                    )
                ]
                for key, label in MARKETING_REPORT_SECTIONS.items()
            ]),
        )
        return

    if action == "status":
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🔄 Проверяю Kaspi Marketing…",
        )

        state, error = await marketing_check_session(shop["id"])
        account = DB.marketing_account(shop["id"])

        if account and state == "login":
            creds = DB.marketing_credentials(shop["id"])
            if creds:
                state, error = await marketing_login_direct(
                    shop["id"], creds[0], creds[1]
                )

        if account:
            DB.update_marketing_status(shop["id"], state, error)

        try:
            await msg.delete()
        except Exception:
            pass

        if state == "otp":
            context.user_data["shop_flow"] = "marketing_otp"
            context.user_data["marketing_shop_id"] = shop["id"]
            await send_marketing_screen(
                context,
                update.effective_chat.id,
                shop["id"],
                "🔐 Kaspi Marketing запросил код. Отправьте код сообщением.",
            )
            return

        if state == "confirmation":
            context.user_data["shop_flow"] = "marketing_confirmation"
            context.user_data["marketing_shop_id"] = shop["id"]
            await send_marketing_screen(
                context,
                update.effective_chat.id,
                shop["id"],
                "🔐 Kaspi Marketing ждёт подтверждение входа.",
            )
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="После подтверждения:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✅ Проверить подтверждение",
                        callback_data="marketing:check",
                    )
                ]]),
            )
            return

        account = DB.marketing_account(shop["id"])
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=marketing_status_text(shop, account),
            parse_mode=ParseMode.HTML,
            reply_markup=marketing_markup(account),
        )
        return

    if action == "check":
        state, error = await marketing_check_confirmation(shop["id"])

        login = context.user_data.get("marketing_login_temp")
        password_enc = context.user_data.get("marketing_password_temp")
        if state == "connected" and login and password_enc:
            DB.save_marketing_account(
                shop["id"],
                login,
                decrypt_token(password_enc),
                status="connected",
            )
        elif DB.marketing_account(shop["id"]):
            DB.update_marketing_status(shop["id"], state, error)

        if state == "connected":
            for key in (
                "shop_flow",
                "marketing_shop_id",
                "marketing_login_temp",
                "marketing_password_temp",
            ):
                context.user_data.pop(key, None)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="✅ Kaspi Marketing подключён.",
                reply_markup=KEYBOARD,
            )
        else:
            await send_marketing_screen(
                context,
                update.effective_chat.id,
                shop["id"],
                "⏳ Подтверждение пока не завершено.",
            )
        return

    if action == "disconnect":
        await _marketing_close(shop["id"])
        for p in (marketing_storage_file(shop["id"]), marketing_screen_file(shop["id"])):
            if p.exists():
                p.unlink()
        DB.delete_marketing_account(shop["id"])

        for key in (
            "shop_flow",
            "marketing_shop_id",
            "marketing_login_temp",
            "marketing_password_temp",
        ):
            context.user_data.pop(key, None)

        await query.edit_message_text(
            f"✅ Kaspi Marketing отключён от <b>{html.escape(shop['name'])}</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=marketing_markup(None),
        )
        return


async def callback_marketing_cabinet(update, context):
    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    shop = DB.active_shop(
        update.effective_user.id
    )
    if not shop:
        return

    try:
        index = int(
            query.data.split(":", 1)[1]
        )
        cabinets = context.user_data.get(
            "marketing_cabinets",
            [],
        )
        cabinet = cabinets[index]
    except Exception:
        await query.edit_message_text(
            "Список кабинетов устарел. Откройте его ещё раз."
        )
        return

    ok = await marketing_select_cabinet(
        shop["id"],
        cabinet,
    )

    if not ok:
        context_diag, page_diag = await _marketing_open_page(
            shop["id"]
        )
        try:
            current_label = await _marketing_current_header_label(
                page_diag
            )
            anchor_diag = await _marketing_open_cabinet_dropdown(
                page_diag
            )
            option_diag = (
                await _marketing_dropdown_options(
                    page_diag,
                    anchor_diag,
                )
                if anchor_diag
                else []
            )
            names_diag = []
            for item in option_diag:
                label = _marketing_clean_text(
                    item.get("text")
                )
                if (
                    label
                    and label.lower()
                    not in {
                        x.lower()
                        for x in names_diag
                    }
                ):
                    names_diag.append(label)

            await _marketing_screen(
                shop["id"],
                page_diag,
            )
        finally:
            await context_diag.close()

        await query.edit_message_text(
            "❌ Не удалось переключить кабинет.\n\n"
            f"Сейчас в шапке: "
            f"<b>{html.escape(str(current_label or 'не определён'))}</b>\n"
            f"Пытались выбрать: "
            f"<b>{html.escape(str(cabinet.get('name') or '—'))}</b>\n\n"
            "Кабинеты, которые вижу в выпадающем списке:\n"
            + (
                "\n".join(
                    f"• {html.escape(name)}"
                    for name in names_diag[:15]
                )
                if names_diag
                else "• список не прочитан"
            ),
            parse_mode=ParseMode.HTML,
        )

        await send_marketing_screen(
            context,
            update.effective_chat.id,
            shop["id"],
            "🖼 Экран Marketing после попытки переключения:",
        )
        return

    DB.set_marketing_cabinet(
        shop["id"],
        cabinet.get("key"),
        cabinet.get("name"),
    )

    await query.edit_message_text(
        f"✅ Выбран кабинет:\n"
        f"<b>{html.escape(str(cabinet.get('name')))}</b>\n\n"
        "Сейчас загружу точные расходы за последние 7 дней. "
        "После этого Marketing будет обновляться автоматически в фоне.",
        parse_mode=ParseMode.HTML,
    )

    today = current_business_date()

    context.application.create_task(
        _marketing_period_sync_worker(
            context.application,
            update.effective_chat.id,
            shop["id"],
            update.effective_user.id,
            today - timedelta(days=6),
            today,
            announce=True,
        ),
        update=update,
        name=f"marketing-7d-sync-{shop['id']}",
    )


async def callback_marketing_report(update, context):

    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    shop = DB.active_shop(
        update.effective_user.id
    )
    if not shop:
        return

    section_key = query.data.split(
        ":",
        1,
    )[1]

    if section_key not in MARKETING_REPORT_SECTIONS:
        return

    campaign_records = await marketing_discover_campaign_records(
        shop["id"],
        section_key,
    )
    campaigns = [
        record["name"]
        for record in campaign_records
    ]

    if not campaigns:
        await query.edit_message_text(
            "⚠️ Не удалось найти кампании в этом разделе."
        )
        await send_marketing_screen(
            context,
            update.effective_chat.id,
            shop["id"],
            "Последний экран Marketing:",
        )
        return

    context.user_data[
        "marketing_report_section"
    ] = section_key
    context.user_data[
        "marketing_campaigns"
    ] = campaigns
    context.user_data[
        "marketing_campaign_records"
    ] = campaign_records

    buttons = []
    for index, name in enumerate(
        campaigns[:15]
    ):
        buttons.append([
            InlineKeyboardButton(
                name[:55],
                callback_data=f"marketcampaign:{index}",
            )
        ])

    await query.edit_message_text(
        "Выберите рекламную кампанию для выгрузки Excel:",
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
    )


async def callback_marketing_campaign(update, context):
    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    shop = DB.active_shop(
        update.effective_user.id
    )
    if not shop:
        return

    try:
        index = int(
            query.data.split(":", 1)[1]
        )
        campaigns = context.user_data[
            "marketing_campaigns"
        ]
        campaign_name = campaigns[index]
        records = context.user_data.get(
            "marketing_campaign_records",
            [],
        )
        campaign = (
            records[index]
            if index < len(records)
            else {"name": campaign_name}
        )
        section_key = context.user_data[
            "marketing_report_section"
        ]
    except Exception:
        await query.edit_message_text(
            "Список кампаний устарел. Откройте отчёты заново."
        )
        return

    result, error = (
        await marketing_download_campaign_report(
            shop["id"],
            section_key,
            campaign,
        )
    )

    if not result:
        await query.edit_message_text(
            "❌ Не удалось скачать отчёт:\n"
            + html.escape(
                error or "неизвестная ошибка"
            ),
            parse_mode=ParseMode.HTML,
        )
        await send_marketing_screen(
            context,
            update.effective_chat.id,
            shop["id"],
            "Последний экран кампании:",
        )
        return

    filename, payload, source_url = result
    file_obj = io.BytesIO(
        payload
    )
    file_obj.name = filename

    account = DB.marketing_account(
        shop["id"]
    )

    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=file_obj,
        filename=filename,
        caption=(
            f"📊 Kaspi Marketing\n"
            f"Кабинет: "
            f"{account['selected_cabinet_name'] if account else '—'}\n"
            f"Кампания: {campaign_name}"
        ),
    )


def settings_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🏪 Магазины / отключение",
                callback_data="settings:shops",
            )
        ],
        [
            InlineKeyboardButton(
                "📣 Marketing / отключение",
                callback_data="settings:marketing",
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Синхронизировать заказы",
                callback_data="settings:syncorders",
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Обновить Marketing за 7 дней",
                callback_data="settings:syncmarketing",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Закрыть",
                callback_data="settings:close",
            )
        ],
    ])


async def cmd_settings(update, context):
    if not await guard(update):
        return

    await update.effective_message.reply_text(
        "⚙️ <b>Настройки</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_markup(),
    )


async def callback_settings(update, context):
    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    action = query.data.split(
        ":",
        1,
    )[1]

    if action == "close":
        await query.edit_message_text(
            "Настройки закрыты."
        )
        return

    if action == "shops":
        await show_shops(
            update,
            context,
            edit=True,
        )
        return

    shop = DB.active_shop(
        update.effective_user.id
    )

    if not shop:
        await query.edit_message_text(
            "Сначала подключите магазин."
        )
        return

    if action == "marketing":
        account = DB.marketing_account(
            shop["id"]
        )
        await query.edit_message_text(
            marketing_status_text(
                shop,
                account,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=marketing_markup(
                account
            ),
        )
        return

    if action == "syncorders":
        shop_id = shop["id"]

        if shop_id in RUNNING_SYNCS:
            await query.edit_message_text(
                "⏳ Заказы этого магазина уже синхронизируются."
            )
            return

        await query.edit_message_text(
            "🔄 Синхронизация заказов запущена."
        )

        status = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"🔄 <b>{html.escape(shop['name'])}</b>: "
                "обновляю заказы в фоне."
            ),
            parse_mode=ParseMode.HTML,
        )

        RUNNING_SYNCS.add(
            shop_id
        )

        context.application.create_task(
            _manual_sync_worker(
                context.application,
                update.effective_chat.id,
                status.message_id,
                shop_id,
                update.effective_user.id,
            ),
            update=update,
            name=f"manual-sync-{shop_id}",
        )
        return

    if action == "syncmarketing":
        account = DB.marketing_account(
            shop["id"]
        )

        if (
            not account
            or not account["selected_cabinet_name"]
        ):
            await query.edit_message_text(
                "Сначала подключите Marketing и выберите кабинет."
            )
            return

        today = current_business_date()

        await query.edit_message_text(
            "🔄 Marketing за 7 дней поставлен в очередь."
        )

        context.application.create_task(
            _marketing_period_sync_worker(
                context.application,
                update.effective_chat.id,
                shop["id"],
                update.effective_user.id,
                today - timedelta(days=6),
                today,
                announce=True,
            ),
            update=update,
            name=f"settings-marketing-sync-{shop['id']}",
        )
        return


def cost_settings_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📥 Выгрузить товары Excel",
            callback_data="costsettings:export",
        )],
        [InlineKeyboardButton(
            "📤 Загрузить заполненный Excel",
            callback_data="costsettings:upload",
        )],
        [InlineKeyboardButton(
            "🧾 Без себестоимости",
            callback_data="costsettings:missing",
        )],
        [InlineKeyboardButton(
            "✍️ Заполнить в Telegram",
            callback_data="costsettings:manual",
        )],
        [InlineKeyboardButton(
            "✏️ Изменить введённую себестоимость",
            callback_data="costsettings:edit",
        )],
        [InlineKeyboardButton(
            "🧮 Комиссии, налог, эквайринг",
            callback_data="costsettings:fees",
        )],
        [InlineKeyboardButton(
            "❌ Закрыть",
            callback_data="costsettings:close",
        )],
    ])


async def cmd_cost_settings(update, context):
    if not await guard(update):
        return

    shop = await require_shop(update)
    if not shop:
        return

    products = DB.catalog_products(shop["id"])
    missing = DB.missing_cost_count(shop["id"])

    await update.effective_message.reply_text(
        f"💰 <b>Себестоимость — {html.escape(shop['name'])}</b>\n\n"
        f"Товаров в базе: <b>{len(products)}</b>\n"
        f"Без себестоимости: <b>{missing}</b>\n\n"
        "Можно выгрузить Excel с фотографиями или нажать "
        "«✍️ Заполнить в Telegram» — товары пойдут автоматически "
        "один за одним.",
        parse_mode=ParseMode.HTML,
        reply_markup=cost_settings_markup(),
    )


async def _fetch_export_photo(shop_id, row):
    sku = row["sku"]
    image_url = gallery_medium_url(row["image_url"])

    if not image_url:
        image_url = gallery_medium_url(
            DB.image_for_sku(shop_id, sku)
        )

    if not image_url and row["kaspi_product_code"]:
        image_url = await resolve_public_product_image(
            row["kaspi_product_code"],
            row["name"],
        )
        if image_url:
            DB.set_image_for_sku(
                shop_id,
                sku,
                image_url,
            )

    if not image_url:
        return None, None

    preview = await download_product_preview(image_url)
    if not preview:
        return image_url, None

    try:
        preview.seek(0)
        with PILImage.open(preview) as img:
            img = img.convert("RGB")
            img.thumbnail((110, 110))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=78, optimize=True)
            return image_url, out.getvalue()
    except Exception as exc:
        print("Excel thumbnail conversion failed:", repr(exc))
        return image_url, None


def _build_cost_excel_bytes(shop_name, products, photo_map):
    wb = Workbook()
    ws = wb.active
    ws.title = "Товары"

    headers = [
        "Фото",
        "SKU",
        "Kaspi код",
        "Название",
        "Цена продажи ₸",
        "Себестоимость ₸",
        "Комиссия %",
        "Налог %",
        "Эквайринг %",
        "Упаковка ₸",
        "Прочие расходы ₸",
        "Заказов",
        "Последний заказ",
        "Фото URL",
    ]
    ws.append(headers)

    header_fill = PatternFill(fill_type="solid", fgColor="1F4E78")
    editable_fill = PatternFill(fill_type="solid", fgColor="FFF2CC")
    header_font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="D9E2F3")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.border = border
        cell.alignment = Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True,
        )

    image_streams = []

    for row_num, row in enumerate(products, start=2):
        last_order = ""
        if row["last_order_ms"]:
            last_order = datetime.fromtimestamp(
                int(row["last_order_ms"]) / 1000,
                TZ,
            ).strftime("%d.%m.%Y %H:%M")

        image_url, thumbnail = photo_map.get(
            str(row["sku"]),
            (None, None),
        )

        ws.append([
            "",
            str(row["sku"] or ""),
            str(row["kaspi_product_code"] or ""),
            str(row["name"] or ""),
            float(row["sale_price"]) if row["sale_price"] is not None else None,
            float(row["cost_price"]) if row["cost_price"] is not None else None,
            float(row["commission_rate"]) if row["commission_rate"] is not None else 0,
            float(row["tax_rate"]) if row["tax_rate"] is not None else 0,
            float(row["acquiring_rate"]) if row["acquiring_rate"] is not None else 0,
            float(row["packaging_cost"]) if row["packaging_cost"] is not None else 0,
            float(row["other_cost"]) if row["other_cost"] is not None else 0,
            int(row["order_count"] or 0),
            last_order,
            image_url or "",
        ])

        ws.row_dimensions[row_num].height = 88

        for col in range(1, 15):
            cell = ws.cell(row_num, col)
            cell.border = border
            cell.alignment = Alignment(
                vertical="center",
                wrap_text=(col in (4, 14)),
            )

        # Sale price is informational/read-only.
        ws.cell(row_num, 5).number_format = '#,##0.00'

        for col in range(6, 12):
            ws.cell(row_num, col).fill = editable_fill
            ws.cell(row_num, col).number_format = '#,##0.00'

        if thumbnail:
            stream = io.BytesIO(thumbnail)
            image_streams.append(stream)
            xl_img = XLImage(stream)
            xl_img.width = 78
            xl_img.height = 78
            ws.add_image(xl_img, f"A{row_num}")

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:N{max(1, ws.max_row)}"
    ws.sheet_view.showGridLines = False

    widths = {
        "A": 14, "B": 20, "C": 18, "D": 40,
        "E": 18,  # Цена продажи
        "F": 18,  # Себестоимость
        "G": 14, "H": 12, "I": 14,
        "J": 16, "K": 20, "L": 10, "M": 20, "N": 62,
    }
    for col, width in widths.items():
        ws.column_dimensions[col].width = width

    info = wb.create_sheet("Инструкция")
    info["A1"] = "Настройки себестоимости"
    info["A1"].font = Font(bold=True, size=16, color="1F4E78")
    instructions = [
        "1. Откройте лист «Товары».",
        "2. «Цена продажи ₸» — последняя фактическая цена SKU из синхронизированных заказов.",
        "3. Жёлтые финансовые колонки можно редактировать.",
        "4. Заполните «Себестоимость ₸» для нужных SKU.",
        "5. SKU менять нельзя — по нему бот сопоставляет товар.",
        "6. Пустая себестоимость пропускается и не превращается в 0.",
        "7. Сохраните файл именно в формате .xlsx.",
        "8. В Telegram откройте «⚙️ Настройки себестоимости» → «📤 Загрузить заполненный Excel».",
        "",
        f"Магазин: {shop_name}",
        "Фото встроено в файл; оригинальный URL находится в колонке «Фото URL».",
    ]
    for idx, value in enumerate(instructions, start=3):
        info.cell(idx, 1).value = value
        info.cell(idx, 1).alignment = Alignment(wrap_text=True)
    info.column_dimensions["A"].width = 100
    info.sheet_view.showGridLines = False

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


async def export_cost_excel(update, context, shop):
    status = await update.effective_message.reply_text(
        "📥 Формирую Excel с товарами и фотографиями…"
    )

    products = list(DB.catalog_products(shop["id"]))

    if not products:
        await status.edit_text(
            "Пока нет товаров для выгрузки. "
            "Сначала выполните синхронизацию."
        )
        return

    photo_map = {}
    semaphore = asyncio.Semaphore(5)

    async def one(row):
        async with semaphore:
            result = await _fetch_export_photo(shop["id"], row)
            return str(row["sku"]), result

    tasks = [asyncio.create_task(one(row)) for row in products]

    done = 0
    for task in asyncio.as_completed(tasks):
        sku, result = await task
        photo_map[sku] = result
        done += 1

        if done % 20 == 0:
            try:
                await status.edit_text(
                    f"📥 Готовлю Excel… {done}/{len(products)} товаров"
                )
            except Exception:
                pass

    excel = await asyncio.to_thread(
        _build_cost_excel_bytes,
        shop["name"],
        products,
        photo_map,
    )

    safe_shop = re.sub(
        r"[^a-zA-Zа-яА-Я0-9_-]+",
        "_",
        shop["name"],
    ).strip("_") or "shop"

    filename = (
        f"sebestoymost_{safe_shop}_"
        f"{datetime.now(TZ):%Y-%m-%d}.xlsx"
    )

    try:
        await status.delete()
    except Exception:
        pass

    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=excel,
        filename=filename,
        caption=(
            f"✅ <b>{html.escape(shop['name'])}</b>\n"
            f"Товаров: <b>{len(products)}</b>\n\n"
            "В таблице добавлена <b>последняя цена продажи</b> каждого SKU.\n"
            "Заполните жёлтую колонку <b>«Себестоимость ₸»</b>, "
            "сохраните файл и загрузите его обратно через настройки."
        ),
        parse_mode=ParseMode.HTML,
    )


def _normalize_excel_header(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


EXCEL_HEADERS = {
    "sku": {"sku", "артикул", "sku / id"},
    "cost_price": {"себестоимость ₸", "себестоимость", "cost_price"},
    "commission_rate": {"комиссия %", "комиссия", "commission_rate"},
    "tax_rate": {"налог %", "налог", "tax_rate"},
    "acquiring_rate": {"эквайринг %", "эквайринг", "acquiring_rate"},
    "packaging_cost": {"упаковка ₸", "упаковка", "packaging_cost"},
    "other_cost": {"прочие расходы ₸", "прочие расходы", "other_cost"},
}


def import_cost_excel_bytes(shop_id, file_bytes):
    wb = load_workbook(io.BytesIO(file_bytes), data_only=True)

    if "Товары" not in wb.sheetnames:
        raise ValueError(
            "Нет листа «Товары». Загрузите Excel, который выгрузил бот."
        )

    ws = wb["Товары"]

    header_map = {}
    for cell in ws[1]:
        normalized = _normalize_excel_header(cell.value)
        for field, variants in EXCEL_HEADERS.items():
            if normalized in variants:
                header_map[field] = cell.column
                break

    if "sku" not in header_map or "cost_price" not in header_map:
        raise ValueError(
            "Не найдены колонки SKU и «Себестоимость ₸»."
        )

    updated = 0
    skipped_blank = 0
    errors = []

    for row_idx in range(2, ws.max_row + 1):
        sku = ws.cell(row_idx, header_map["sku"]).value
        if sku is None or str(sku).strip() == "":
            continue

        cost_value = ws.cell(
            row_idx,
            header_map["cost_price"],
        ).value

        if cost_value is None or str(cost_value).strip() == "":
            skipped_blank += 1
            continue

        kwargs = {}
        for field in (
            "commission_rate",
            "tax_rate",
            "acquiring_rate",
            "packaging_cost",
            "other_cost",
        ):
            col = header_map.get(field)
            kwargs[field] = ws.cell(row_idx, col).value if col else None

        try:
            if DB.upsert_cost_partial(
                shop_id,
                str(sku).strip(),
                cost_price=cost_value,
                **kwargs,
            ):
                updated += 1
        except Exception as exc:
            errors.append(f"строка {row_idx}, SKU {sku}: {exc}")

    return {
        "updated": updated,
        "skipped_blank": skipped_blank,
        "errors": errors[:10],
    }


async def show_cost_edit_list(update, context, shop, offset=0):
    limit = 8
    total = DB.filled_cost_count(
        shop["id"]
    )
    rows = DB.filled_cost_examples(
        shop["id"],
        limit=limit,
        offset=offset,
    )

    if not rows:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Пока нет заполненной себестоимости для редактирования.",
            reply_markup=cost_settings_markup(),
        )
        return

    buttons = []

    for row in rows:
        item_rowid = row["item_rowid"]
        if item_rowid is None:
            continue

        label = (
            f"{str(row['name'])[:26]} — "
            f"{money(float(row['cost_price']))}"
        )
        buttons.append([
            InlineKeyboardButton(
                label,
                callback_data=f"costedit:{int(item_rowid)}",
            )
        ])

    nav = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️",
                callback_data=f"costeditpage:{max(0, offset-limit)}",
            )
        )
    if offset + limit < total:
        nav.append(
            InlineKeyboardButton(
                "➡️",
                callback_data=f"costeditpage:{offset+limit}",
            )
        )
    if nav:
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Настройки",
            callback_data="costeditback",
        )
    ])

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"✏️ <b>Изменить себестоимость</b>\n"
            f"Заполнено SKU: <b>{total}</b>\n\n"
            "Выберите товар:"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
    )


async def callback_cost_edit(update, context):
    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    shop = DB.active_shop(
        update.effective_user.id
    )
    if not shop:
        await query.edit_message_text(
            "Сначала подключите магазин."
        )
        return

    data = query.data

    if data == "costeditback":
        await query.edit_message_text(
            "⚙️ Настройки себестоимости",
            reply_markup=cost_settings_markup(),
        )
        return

    if data.startswith("costeditpage:"):
        offset = int(
            data.split(":", 1)[1]
        )
        try:
            await query.delete_message()
        except Exception:
            pass
        await show_cost_edit_list(
            update,
            context,
            shop,
            offset=offset,
        )
        return

    if data.startswith("costedit:"):
        rowid = int(
            data.split(":", 1)[1]
        )
        item = DB.item_by_rowid(
            shop["id"],
            rowid,
        )
        if not item:
            await query.edit_message_text(
                "Товар не найден."
            )
            return

        cost = DB.cost_for_item(shop["id"], item)
        if not cost:
            await query.edit_message_text(
                "Для этого SKU себестоимость уже отсутствует."
            )
            return

        context.user_data["shop_flow"] = (
            "cost_edit_input"
        )
        context.user_data["cost_shop_id"] = (
            shop["id"]
        )
        context.user_data["cost_rowid"] = rowid

        caption = (
            f"✏️ <b>Редактирование себестоимости</b>\n\n"
            f"Товар: <b>{html.escape(item['name'] or 'Без названия')}</b>\n"
            f"SKU: <code>{html.escape(str(item['sku']))}</code>\n"
            f"Категория: <b>{html.escape(cost_product_type(item['name'], item['category']))}</b>\n"
            f"Текущая себестоимость: "
            f"<b>{money(float(cost['cost_price']))}</b>\n\n"
            "Отправьте себестоимость ОДНОЙ штуки.\n"
            "Например: <code>1650</code>\n\n"
            "Или нажмите <b>⬅️ Назад</b>."
        )

        image_url = gallery_medium_url(
            item["image_url"]
            or DB.image_for_sku(
                shop["id"],
                item["sku"],
            )
        )

        if image_url:
            preview = await download_product_preview(
                image_url
            )
            if preview:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=preview,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=COST_INPUT_KEYBOARD,
                )
                return

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=COST_INPUT_KEYBOARD,
        )


def financial_settings_markup(shop):
    settings = DB.financial_settings(
        shop["id"]
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"Kaspi: {float(settings['default_commission_rate']):.2f}%",
                callback_data="fees:commission",
            )
        ],
        [
            InlineKeyboardButton(
                f"Налог: {float(settings['default_tax_rate']):.2f}%",
                callback_data="fees:tax",
            )
        ],
        [
            InlineKeyboardButton(
                f"Эквайринг: {float(settings['default_acquiring_rate']):.2f}%",
                callback_data="fees:acquiring",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Настройки себестоимости",
                callback_data="fees:back",
            )
        ],
    ])


async def show_financial_settings(update, context, shop):
    settings = DB.financial_settings(
        shop["id"]
    )

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"🧮 <b>Комиссии и налоги — "
            f"{html.escape(shop['name'])}</b>\n\n"
            f"Комиссия Kaspi по умолчанию: "
            f"<b>{float(settings['default_commission_rate']):.2f}%</b>\n"
            f"Налог: <b>{float(settings['default_tax_rate']):.2f}%</b>\n"
            f"Эквайринг: "
            f"<b>{float(settings['default_acquiring_rate']):.2f}%</b>\n\n"
            "Если у SKU в Excel указан свой ненулевой процент, "
            "он имеет приоритет.\n"
            "Для категорий головных уборов/шарфов/перчаток "
            "бот автоматически использует 15.5% Kaspi, "
            "если у SKU ставка не задана."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=financial_settings_markup(
            shop
        ),
    )


async def callback_financial_settings(update, context):
    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    shop = DB.active_shop(
        update.effective_user.id
    )
    if not shop:
        await query.edit_message_text(
            "Сначала подключите магазин."
        )
        return

    action = query.data.split(":", 1)[1]

    if action == "back":
        await query.edit_message_text(
            "⚙️ Настройки себестоимости",
            reply_markup=cost_settings_markup(),
        )
        return

    fields = {
        "commission": (
            "default_commission_rate",
            "комиссию Kaspi",
        ),
        "tax": (
            "default_tax_rate",
            "налог",
        ),
        "acquiring": (
            "default_acquiring_rate",
            "эквайринг",
        ),
    }

    if action not in fields:
        return

    field, label = fields[action]
    context.user_data["shop_flow"] = (
        "financial_setting_input"
    )
    context.user_data["financial_field"] = field
    context.user_data["financial_label"] = label
    context.user_data["financial_shop_id"] = (
        shop["id"]
    )

    await query.edit_message_text(
        f"Введите {label} в процентах.\n\n"
        "Например: <code>15.5</code>",
        parse_mode=ParseMode.HTML,
    )

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Процент:",
        reply_markup=COST_INPUT_KEYBOARD,
    )


async def callback_cost_settings(update, context):
    query = update.callback_query
    await query.answer()

    if not await guard(update):
        return

    shop = DB.active_shop(update.effective_user.id)
    if not shop:
        await query.edit_message_text("Сначала подключите магазин.")
        return

    action = query.data.split(":", 1)[1]

    if action == "close":
        await query.edit_message_text("Настройки себестоимости закрыты.")
        return

    if action == "missing":
        await show_missing_costs(
            update,
            context,
        )
        return

    if action == "manual":
        # Start a global continuous queue.
        context.user_data.pop("shop_flow", None)
        context.user_data.pop("cost_shop_id", None)
        context.user_data.pop("cost_rowid", None)
        context.user_data.pop("cost_scope_start_ms", None)
        context.user_data.pop("cost_scope_end_ms", None)
        context.user_data.pop("cost_scope_label", None)

        try:
            await query.edit_message_text(
                "✍️ Последовательное заполнение запущено."
            )
        except Exception:
            pass

        await send_next_cost_input(
            update,
            context,
            shop,
        )
        return

    if action == "fees":
        await show_financial_settings(
            update,
            context,
            shop,
        )
        return

    if action == "edit":
        await show_cost_edit_list(
            update,
            context,
            shop,
            offset=0,
        )
        return

    if action == "upload":
        context.user_data["awaiting_cost_xlsx"] = True
        await query.edit_message_text(
            "📤 <b>Загрузка себестоимости</b>\n\n"
            "Отправьте заполненный файл <b>.xlsx</b>, "
            "который ранее выгрузил бот.\n\n"
            "Однозначные SKU сопоставляются автоматически. "
            "Повторяющиеся у разных моделей или категорий SKU пропускаются: "
            "заполните их в Telegram, чтобы не смешивать цены.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "export":
        await query.edit_message_text("📥 Начинаю выгрузку товаров…")
        await export_cost_excel(update, context, shop)
        return


async def cmd_costs(update, context):
    return await cmd_cost_settings(update, context)


async def cmd_cost_settings_command(update, context):
    return await cmd_cost_settings(update, context)


async def cmd_status(update, context):
    if not await guard(update):
        return

    shop = await require_shop(update)
    if not shop:
        return

    stats = DB.status_stats(shop["id"])

    await update.message.reply_text(
        f"🏪 {shop['name']}\n"
        f"📦 Заказов в БД: {stats['orders_count']}\n"
        f"🧾 Позиций: {stats['item_count']}\n"
        f"💰 SKU с себестоимостью: {stats['cost_count']}\n"
        f"🕒 Последнее обновление: "
        f"{stats['last_seen'] or '—'}"
    )


async def handle_document(update, context):
    if not await guard(update):
        return

    shop = await require_shop(update)
    if not shop:
        return

    doc = update.message.document
    filename = (doc.file_name or "").lower()

    tg_file = await context.bot.get_file(doc.file_id)
    data = bytes(await tg_file.download_as_bytearray())

    if filename.endswith(".xlsx"):
        try:
            result = await asyncio.to_thread(
                import_cost_excel_bytes,
                shop["id"],
                data,
            )

            context.user_data.pop("awaiting_cost_xlsx", None)
            remaining = DB.missing_cost_count(shop["id"])

            msg = (
                f"✅ <b>{html.escape(shop['name'])}</b>\n"
                f"Обновлено SKU: <b>{result['updated']}</b>\n"
                f"Пустых строк пропущено: <b>{result['skipped_blank']}</b>\n"
                f"Осталось без себестоимости: <b>{remaining}</b>"
            )

            if result["errors"]:
                msg += "\n\n⚠️ Ошибки:\n" + "\n".join(
                    html.escape(x) for x in result["errors"]
                )

            await update.message.reply_text(
                msg,
                parse_mode=ParseMode.HTML,
                reply_markup=cost_settings_markup(),
            )

        except Exception as exc:
            await update.message.reply_text(
                f"❌ Ошибка импорта Excel: {exc}\n\n"
                "Загрузите файл, который был создан через "
                "«📥 Выгрузить товары Excel»."
            )
        return

    # Backward compatibility with old CSV workflow.
    if filename.endswith(".csv"):
        try:
            csv_text = data.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(csv_text))

            required = {
                "sku", "cost_price", "commission_rate",
                "tax_rate", "packaging_cost", "other_cost",
            }

            if not required.issubset(set(reader.fieldnames or [])):
                await update.message.reply_text(
                    "Неверные колонки CSV.\nНужны:\n"
                    + ", ".join(sorted(required))
                )
                return

            count = DB.upsert_costs(shop["id"], list(reader))

            await update.message.reply_text(
                f"✅ {shop['name']}: себестоимость обновлена для {count} SKU."
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ Ошибка импорта CSV: {exc}")
        return

    await update.message.reply_text(
        "Для загрузки себестоимости отправьте .xlsx из выгрузки бота."
    )


async def handle_shop_flow(update, context):
    flow = context.user_data.get("shop_flow")

    if not flow:
        return False

    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    if flow == "cost_group_confirm":
        if text.lower() in {"назад", "⬅️ назад", "/cancel"}:
            context.user_data.pop("proposed_cost", None)
            context.user_data["shop_flow"] = "cost_input"
            await update.message.reply_text("Выбор отменён. Отправьте другую цену или «Назад».")
        else:
            await update.message.reply_text("Подтвердите цену кнопкой «Все цвета модели» или «Только этот товар» под предыдущим сообщением.")
        return True

    if flow == "cost_input":
        shop_id = context.user_data.get("cost_shop_id")
        rowid = context.user_data.get("cost_rowid")
        shop = DB.shop(shop_id, user_id) if shop_id else None
        item = DB.item_by_rowid(shop_id, rowid) if shop and rowid else None

        # User can leave sequential cost entry at any moment without
        # saving anything for the currently displayed SKU.
        if text.strip().lower() in {
            "⬅️ назад",
            "назад",
            "/back",
            "/cancel",
        }:
            context.user_data.pop("shop_flow", None)
            context.user_data.pop("cost_shop_id", None)
            context.user_data.pop("cost_rowid", None)
            context.user_data.pop("cost_scope_start_ms", None)
            context.user_data.pop("cost_scope_end_ms", None)
            context.user_data.pop("cost_scope_label", None)

            await update.message.reply_text(
                "⬅️ Последовательное заполнение остановлено.",
                reply_markup=KEYBOARD,
            )

            if shop:
                products = DB.catalog_products(shop["id"])
                missing = DB.missing_cost_count(shop["id"])

                await update.message.reply_text(
                    f"⚙️ <b>Настройки себестоимости — "
                    f"{html.escape(shop['name'])}</b>\n\n"
                    f"Товаров в базе: <b>{len(products)}</b>\n"
                    f"Без себестоимости: <b>{missing}</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=cost_settings_markup(),
                )

            return True

        if not shop or not item:
            context.user_data.clear()
            await update.message.reply_text(
                "Не удалось найти магазин или товар. "
                "Откройте «Без себестоимости» ещё раз."
            )
            return True

        normalized = (
            text.lower()
            .replace("₸", "")
            .replace("тг", "")
            .replace(" ", "")
            .replace(",", ".")
        )

        try:
            value = float(normalized)
        except ValueError:
            await update.message.reply_text(
                "Введите только число. Например: 1600"
            )
            return True

        if value < 0 or value > 100_000_000:
            await update.message.reply_text(
                "Проверьте сумму и отправьте её ещё раз."
            )
            return True

        variants = DB.same_model_products(shop_id, item)
        key = model_cost_group_key(item["name"], item["category"])
        if key and len(variants) > 1:
            context.user_data["shop_flow"] = "cost_group_confirm"
            context.user_data["proposed_cost"] = value
            sample = ", ".join(str(row["name"] or "Товар") for row in variants[:4])
            await update.message.reply_text(
                f"💰 <b>{html.escape(item['name'] or 'Товар')}</b>\n"
                f"Категория: <b>{html.escape(cost_product_type(item['name'], item['category']))}</b>\n"
                f"Себестоимость: <b>{money(value)}</b> за 1 шт.\n"
                f"Найдено вариантов модели: <b>{len(variants)}</b>\n"
                f"Примеры: {html.escape(sample)}\n\n"
                "Применить одинаковую цену ко всем цветам модели? "
                "Другие категории и модели не затрагиваются.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Все цвета модели",callback_data="costgroup:all")],
                    [InlineKeyboardButton("🔹 Только этот товар",callback_data="costgroup:one")],
                    [InlineKeyboardButton("❌ Отмена",callback_data="costgroup:cancel")],
                ]),
            )
            return True

        # A safe, recognized model can inherit this price for colours not yet sold.
        # Unrecognizable/ambiguous model names remain strictly per-item.
        DB.set_cost_for_item(shop_id, item, value, whole_model=bool(key))

        # Clear only the current SKU pointer. The sequential flow
        # continues immediately with the next missing SKU.
        context.user_data.pop("cost_rowid", None)

        scope_start_ms = context.user_data.get(
            "cost_scope_start_ms"
        )
        scope_end_ms = context.user_data.get(
            "cost_scope_end_ms"
        )
        scope_label = context.user_data.get(
            "cost_scope_label"
        )

        if scope_start_ms is not None and scope_end_ms is not None:
            remaining = DB.missing_cost_count_for_report_period(
                shop_id,
                scope_start_ms,
                scope_end_ms,
            )
        else:
            remaining = DB.missing_cost_count(
                shop_id
            )

        await update.message.reply_text(
            f"✅ <b>{html.escape(item['name'] or 'Товар')}</b> — "
            f"<b>{money(value)}</b>\n"
            f"Осталось: <b>{remaining}</b>",
            parse_mode=ParseMode.HTML,
        )

        if remaining:
            await send_next_cost_input(
                update,
                context,
                shop,
            )
        else:
            context.user_data.pop(
                "shop_flow",
                None,
            )
            context.user_data.pop(
                "cost_shop_id",
                None,
            )
            context.user_data.pop(
                "cost_scope_start_ms",
                None,
            )
            context.user_data.pop(
                "cost_scope_end_ms",
                None,
            )
            context.user_data.pop(
                "cost_scope_label",
                None,
            )

            done_text = (
                f"Себестоимость заполнена для всех SKU отчёта "
                f"{html.escape(scope_label)}."
                if scope_label
                else "Себестоимость заполнена для всех найденных SKU."
            )

            await update.message.reply_text(
                "🎉 <b>Готово.</b> " + done_text,
                parse_mode=ParseMode.HTML,
                reply_markup=KEYBOARD,
            )

            await update.message.reply_text(
                "💰 Себестоимость:",
                reply_markup=cost_settings_markup(),
            )

        return True


    if flow == "returns_period_input":
        if text.lower() in {"назад", "⬅️ назад", "/cancel"}:
            context.user_data.pop("shop_flow", None)
            await update.message.reply_text("Выбор периода отменён.", reply_markup=KEYBOARD)
            return True
        parsed = parse_period_text(text)
        if not parsed or len(text.split()) < 2:
            await update.message.reply_text(
                "Нужны две даты. Например: 01.10.2026 23.10.2026. "
                "Для отмены напишите «Назад»."
            )
            return True
        context.user_data.pop("shop_flow", None)
        a, b = parsed
        return await send_returns_report(update, context, a, b)

    if flow == "marketing_login":
        shop_id = context.user_data.get("marketing_shop_id")
        shop = DB.shop(shop_id, user_id) if shop_id else None

        if text.lower() in {"⬅️ назад", "назад", "/back", "/cancel"}:
            context.user_data.pop("shop_flow", None)
            context.user_data.pop("marketing_shop_id", None)
            context.user_data.pop("marketing_login_temp", None)
            await update.message.reply_text(
                "⬅️ Подключение Marketing отменено.",
                reply_markup=KEYBOARD,
            )
            return True

        phone = normalize_marketing_phone(
            text
        )

        if not shop or not phone:
            await update.message.reply_text(
                "Введите 10 цифр номера телефона без 8 и +7.\n"
                "Например: 7011234567",
                reply_markup=COST_INPUT_KEYBOARD,
            )
            return True

        context.user_data["marketing_login_temp"] = phone
        context.user_data["shop_flow"] = "marketing_password"

        await update.message.reply_text(
            "Теперь отправьте пароль Kaspi Marketing.\n"
            "Сообщение с паролем бот сразу удалит.",
            reply_markup=COST_INPUT_KEYBOARD,
        )
        return True

    if flow == "marketing_password":
        shop_id = context.user_data.get("marketing_shop_id")
        shop = DB.shop(shop_id, user_id) if shop_id else None

        if text.lower() in {"⬅️ назад", "назад", "/back", "/cancel"}:
            context.user_data.pop("shop_flow", None)
            context.user_data.pop("marketing_shop_id", None)
            context.user_data.pop("marketing_login_temp", None)
            await update.message.reply_text(
                "⬅️ Подключение Marketing отменено.",
                reply_markup=KEYBOARD,
            )
            return True

        login = context.user_data.get("marketing_login_temp")
        password = text

        try:
            await update.message.delete()
        except Exception:
            pass

        if not shop or not login:
            context.user_data.clear()
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Сессия подключения потеряна. Начните заново.",
                reply_markup=KEYBOARD,
            )
            return True

        status = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🔐 Вхожу напрямую в marketing.kaspi.kz…",
        )

        state, error = await marketing_login_direct(
            shop["id"], login, password
        )
        context.user_data["marketing_password_temp"] = encrypt_token(password)

        try:
            await status.delete()
        except Exception:
            pass

        if state == "connected":
            DB.save_marketing_account(
                shop["id"], login, password, status="connected"
            )
            for key in (
                "shop_flow",
                "marketing_shop_id",
                "marketing_login_temp",
                "marketing_password_temp",
            ):
                context.user_data.pop(key, None)

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="✅ Kaspi Marketing подключён.",
                reply_markup=KEYBOARD,
            )
            return True

        if state == "otp":
            context.user_data["shop_flow"] = "marketing_otp"
            await send_marketing_screen(
                context,
                update.effective_chat.id,
                shop["id"],
                "🔐 Kaspi Marketing запросил код. Отправьте код сообщением.",
            )
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Код:",
                reply_markup=COST_INPUT_KEYBOARD,
            )
            return True

        if state == "confirmation":
            context.user_data["shop_flow"] = "marketing_confirmation"
            await send_marketing_screen(
                context,
                update.effective_chat.id,
                shop["id"],
                "🔐 Kaspi Marketing ждёт подтверждение входа.",
            )
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="После подтверждения нажмите:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✅ Проверить подтверждение",
                        callback_data="marketing:check",
                    )
                ]]),
            )
            return True

        await _marketing_close(shop["id"])
        for key in (
            "shop_flow",
            "marketing_shop_id",
            "marketing_login_temp",
            "marketing_password_temp",
        ):
            context.user_data.pop(key, None)

        await send_marketing_screen(
            context,
            update.effective_chat.id,
            shop["id"],
            "❌ Вход не выполнен.\n" + html.escape(error or state),
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Попробуйте ещё раз.",
            reply_markup=KEYBOARD,
        )
        return True

    if flow == "marketing_otp":
        shop_id = context.user_data.get("marketing_shop_id")
        shop = DB.shop(shop_id, user_id) if shop_id else None

        if text.lower() in {"⬅️ назад", "назад", "/back", "/cancel"}:
            if shop_id:
                await _marketing_close(shop_id)
            for key in (
                "shop_flow",
                "marketing_shop_id",
                "marketing_login_temp",
                "marketing_password_temp",
            ):
                context.user_data.pop(key, None)
            await update.message.reply_text(
                "⬅️ Авторизация отменена.",
                reply_markup=KEYBOARD,
            )
            return True

        if not shop:
            return True

        state, error = await marketing_submit_code(shop["id"], text)

        if state == "connected":
            login = context.user_data.get("marketing_login_temp")
            password_enc = context.user_data.get("marketing_password_temp")
            if login and password_enc:
                DB.save_marketing_account(
                    shop["id"],
                    login,
                    decrypt_token(password_enc),
                    status="connected",
                )

            for key in (
                "shop_flow",
                "marketing_shop_id",
                "marketing_login_temp",
                "marketing_password_temp",
            ):
                context.user_data.pop(key, None)

            await update.message.reply_text(
                "✅ Kaspi Marketing подключён.",
                reply_markup=KEYBOARD,
            )
            return True

        await send_marketing_screen(
            context,
            update.effective_chat.id,
            shop["id"],
            "⚠️ Авторизация ещё не завершена. "
            + html.escape(error or state),
        )
        return True

    if flow == "marketing_confirmation":
        if text.lower() in {"⬅️ назад", "назад", "/back", "/cancel"}:
            shop_id = context.user_data.get("marketing_shop_id")
            if shop_id:
                await _marketing_close(shop_id)
            for key in (
                "shop_flow",
                "marketing_shop_id",
                "marketing_login_temp",
                "marketing_password_temp",
            ):
                context.user_data.pop(key, None)
            await update.message.reply_text(
                "⬅️ Авторизация отменена.",
                reply_markup=KEYBOARD,
            )
            return True

        await update.message.reply_text(
            "Подтвердите вход и нажмите «✅ Проверить подтверждение».",
            reply_markup=COST_INPUT_KEYBOARD,
        )
        return True

    if flow == "financial_setting_input":
        shop_id = context.user_data.get(
            "financial_shop_id"
        )
        shop = (
            DB.shop(shop_id, user_id)
            if shop_id
            else None
        )

        if text.strip().lower() in {
            "⬅️ назад",
            "назад",
            "/back",
            "/cancel",
        }:
            for key in (
                "shop_flow",
                "financial_field",
                "financial_label",
                "financial_shop_id",
            ):
                context.user_data.pop(
                    key,
                    None,
                )

            await update.message.reply_text(
                "⬅️ Изменение ставки отменено.",
                reply_markup=KEYBOARD,
            )

            if shop:
                await show_financial_settings(
                    update,
                    context,
                    shop,
                )
            return True

        if not shop:
            context.user_data.clear()
            return True

        normalized = (
            text.replace("%", "")
            .replace(",", ".")
            .replace(" ", "")
        )

        try:
            value = float(
                normalized
            )
        except ValueError:
            await update.message.reply_text(
                "Введите процент числом. Например: 15.5",
                reply_markup=COST_INPUT_KEYBOARD,
            )
            return True

        if value < 0 or value > 100:
            await update.message.reply_text(
                "Процент должен быть от 0 до 100.",
                reply_markup=COST_INPUT_KEYBOARD,
            )
            return True

        field = context.user_data.get(
            "financial_field"
        )
        label = context.user_data.get(
            "financial_label"
        )

        DB.update_financial_setting(
            shop["id"],
            field,
            value,
        )

        for key in (
            "shop_flow",
            "financial_field",
            "financial_label",
            "financial_shop_id",
        ):
            context.user_data.pop(
                key,
                None,
            )

        await update.message.reply_text(
            f"✅ {label}: <b>{value:.2f}%</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=KEYBOARD,
        )

        await show_financial_settings(
            update,
            context,
            shop,
        )
        return True

    if flow == "cost_edit_input":
        shop_id = context.user_data.get(
            "cost_shop_id"
        )
        rowid = context.user_data.get(
            "cost_rowid"
        )
        shop = (
            DB.shop(shop_id, user_id)
            if shop_id
            else None
        )
        item = (
            DB.item_by_rowid(
                shop_id,
                rowid,
            )
            if shop and rowid
            else None
        )

        if text.strip().lower() in {
            "⬅️ назад",
            "назад",
            "/back",
            "/cancel",
        }:
            context.user_data.pop(
                "shop_flow",
                None,
            )
            context.user_data.pop(
                "cost_shop_id",
                None,
            )
            context.user_data.pop(
                "cost_rowid",
                None,
            )

            await update.message.reply_text(
                "⬅️ Редактирование отменено.",
                reply_markup=KEYBOARD,
            )

            if shop:
                await show_cost_edit_list(
                    update,
                    context,
                    shop,
                    offset=0,
                )
            return True

        if not shop or not item:
            context.user_data.clear()
            await update.message.reply_text(
                "Не удалось найти товар.",
                reply_markup=KEYBOARD,
            )
            return True

        normalized = (
            text.lower()
            .replace("₸", "")
            .replace("тг", "")
            .replace(" ", "")
            .replace(",", ".")
        )

        try:
            value = float(
                normalized
            )
        except ValueError:
            await update.message.reply_text(
                "Введите число. Например: 1650",
                reply_markup=COST_INPUT_KEYBOARD,
            )
            return True

        if value < 0 or value > 100_000_000:
            await update.message.reply_text(
                "Проверьте сумму.",
                reply_markup=COST_INPUT_KEYBOARD,
            )
            return True

        DB.set_cost_for_item(shop_id, item, value, whole_model=False)

        context.user_data.pop(
            "shop_flow",
            None,
        )
        context.user_data.pop(
            "cost_shop_id",
            None,
        )
        context.user_data.pop(
            "cost_rowid",
            None,
        )

        await update.message.reply_text(
            f"✅ <b>{html.escape(item['name'] or 'Товар')}</b>\n"
            f"SKU: <code>{html.escape(str(item['sku']))}</code>\n"
            f"Новая себестоимость: <b>{money(value)}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=KEYBOARD,
        )

        await show_cost_edit_list(
            update,
            context,
            shop,
            offset=0,
        )
        return True

    if flow == "add_name":
        if len(text) < 2:
            await update.message.reply_text(
                "Название слишком короткое."
            )
            return True

        context.user_data["new_shop_name"] = text
        context.user_data["shop_flow"] = "add_token"

        await update.message.reply_text(
            f"🏪 Название: <b>{html.escape(text)}</b>\n\n"
            "Теперь отправьте X-Auth-Token Kaspi "
            "одним сообщением.\n\n"
            "Сообщение с токеном бот сразу попытается удалить.",
            parse_mode=ParseMode.HTML,
        )
        return True

    if flow in {"add_token", "edit_token"}:
        token = text

        # Try to remove the secret from Telegram immediately.
        try:
            await update.message.delete()
        except Exception:
            pass

        status_message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🔐 Проверяю токен Kaspi…",
        )

        if len(token) < 10:
            await status_message.edit_text(
                "❌ Токен выглядит некорректно. "
                "Попробуйте ещё раз."
            )
            return True

        try:
            await KaspiAPI(token).validate()

        except httpx.HTTPStatusError as exc:
            await status_message.edit_text(
                f"❌ Kaspi API отклонил токен "
                f"(HTTP {exc.response.status_code}).\n"
                "Отправьте другой токен."
            )
            return True

        except Exception as exc:
            await status_message.edit_text(
                "❌ Не удалось проверить токен: "
                f"{type(exc).__name__}: {exc}\n"
                "Попробуйте ещё раз."
            )
            return True

        if flow == "add_token":
            name = context.user_data["new_shop_name"]

            if DB.shop_count_for_user(user_id) >= MAX_SHOPS_PER_USER:
                context.user_data.clear()
                await status_message.edit_text(
                    f"Достигнут лимит: {MAX_SHOPS_PER_USER} магазинов на пользователя."
                )
                return True

            DB.add_shop(
                user_id,
                name,
                token,
            )

            context.user_data.clear()

            await status_message.edit_text(
                f"✅ Магазин <b>{html.escape(name)}</b> подключён.\n\n"
                "Токен проверен и сохранён зашифрованным.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🏪 Магазины",
                            callback_data="shopsback",
                        )
                    ]
                ]),
            )
            return True

        shop_id = context.user_data["shop_id"]
        shop = DB.shop(shop_id, user_id)

        if not shop:
            context.user_data.clear()
            await status_message.edit_text(
                "Магазин уже не существует."
            )
            return True

        DB.update_shop_token(
            user_id,
            shop_id,
            token,
        )

        context.user_data.clear()

        await status_message.edit_text(
            f"✅ Токен магазина <b>{html.escape(shop['name'])}</b> "
            "обновлён и проверен.",
            parse_mode=ParseMode.HTML,
        )
        return True

    if flow == "rename":
        shop_id = context.user_data["shop_id"]
        shop = DB.shop(shop_id, user_id)

        if not shop:
            context.user_data.clear()
            await update.message.reply_text(
                "Магазин уже не существует."
            )
            return True

        if len(text) < 2:
            await update.message.reply_text(
                "Название слишком короткое."
            )
            return True

        DB.rename_shop(
            user_id,
            shop_id,
            text,
        )

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ Магазин переименован: <b>{html.escape(text)}</b>",
            parse_mode=ParseMode.HTML,
        )
        return True

    return False


async def handle_text(update, context):
    if not await guard(update):
        return

    if await handle_shop_flow(update, context):
        return

    text = update.message.text.strip()
    low = text.lower()

    if low in {
        "📊 отчет за сегодня",
        "📊 сегодня",
    }:
        return await cmd_today(update, context)

    if low in {
        "📈 отчет за неделю",
        "📈 неделя",
    }:
        return await cmd_week(update, context)

    if low == "📅 вчера":
        return await cmd_yesterday(update, context)

    if low in {"📆 дата", "📆 выбрать дату"}:
        return await show_calendar(
            update,
            context,
            mode="single",
        )

    if low in {"🗓 период", "🗓 выбрать период"}:
        context.user_data.pop("period_start", None)
        return await show_calendar(
            update,
            context,
            mode="period_start",
        )

    if low in {"↩️ возвраты", "возвраты"}:
        return await cmd_returns(update, context)

    if low == "⚙️ настройки":
        return await cmd_settings(update, context)

    if low in {
        "💰 себестоимость",
        "⚙️ настройки себестоимости",
    }:
        return await cmd_cost_settings(update, context)

    # Old keyboard labels/commands remain accepted but are no longer shown.
    if low == "🏪 магазины":
        return await cmd_shops(update, context)

    if low == "🔄 синхронизация":
        return await cmd_sync(update, context)

    if low == "📣 маркетинг":
        return await cmd_marketing(update, context)

    # Old keyboard labels remain accepted.
    if low == "🧾 без себестоимости":
        return await cmd_missing(update, context)

    if low == "💰 себестоимость csv":
        return await cmd_cost_settings(update, context)

    shop = await require_shop(update)

    if not shop:
        return

    parsed = parse_period_text(text)

    if parsed:
        a, b = parsed

        await send_date_report(
            update,
            context,
            a,
            b,
        )
        return

    await update.message.reply_text(
        "Пока MVP понимает даты и периоды.\n"
        "Например: «1 сентября», "
        "«с 1 по 5 сентября», «сегодня»."
    )


async def scheduled_sync(context):
    shops = DB.all_shops()

    for shop in shops:
        shop_id = shop["id"]

        if shop_id in RUNNING_SYNCS:
            continue

        RUNNING_SYNCS.add(shop_id)

        try:
            await sync_shop(shop)
        except Exception as exc:
            print(
                "Scheduled sync error "
                f"shop={shop_id}: {repr(exc)}"
            )
        finally:
            RUNNING_SYNCS.discard(shop_id)


async def scheduled_marketing_sync(context):
    """
    After one-time cabinet setup, Marketing keeps refreshing automatically.
    No repeated login prompt: saved session/credentials are reused.
    """
    shops = DB.all_shops()

    for shop in shops:
        shop_id = shop["id"]

        if shop_id in RUNNING_MARKETING_SYNCS:
            continue

        account = DB.marketing_account(
            shop_id
        )

        if (
            not account
            or not account["selected_cabinet_name"]
        ):
            continue

        RUNNING_MARKETING_SYNCS.add(
            shop_id
        )

        try:
            today = current_business_date()
            await marketing_sync_exact_period(
                shop_id,
                today - timedelta(days=1),
                today,
            )
        except Exception as exc:
            print(
                "Scheduled Marketing sync error "
                f"shop={shop_id}: {repr(exc)}"
            )
        finally:
            RUNNING_MARKETING_SYNCS.discard(
                shop_id
            )


async def telegram_error_handler(update, context):
    error = context.error

    if isinstance(error, Conflict):
        print(
            "\n[TELEGRAM CONFLICT] Другой экземпляр бота уже работает "
            "с этим же TELEGRAM_BOT_TOKEN.\n"
            "Закройте второе окно/процесс бота и оставьте только один.\n"
            f"Details: {repr(error)}\n"
        )
        return

    if isinstance(error, (NetworkError, TimedOut)):
        print(
            "Telegram network problem; polling/commands will retry "
            f"when connection returns: {repr(error)}"
        )
        return

    print("Unhandled Telegram handler error:", repr(error))


_INSTANCE_SOCKET = None


def acquire_local_instance_lock():
    """
    Prevent two copies of the same bot token from running on this PC.
    Uses a localhost TCP port derived from the token hash.
    The OS releases it automatically when the process exits.
    """
    global _INSTANCE_SOCKET

    if not TELEGRAM_BOT_TOKEN:
        return

    digest = hashlib.sha256(
        TELEGRAM_BOT_TOKEN.encode("utf-8")
    ).digest()

    # Stable private high port 42000..51999.
    port = 42000 + (
        int.from_bytes(
            digest[:4],
            "big",
        )
        % 10000
    )

    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM,
    )

    # Do not enable SO_REUSEADDR: exclusive bind is intentional.
    try:
        sock.bind(
            ("127.0.0.1", port)
        )
        sock.listen(1)
    except OSError as exc:
        sock.close()
        raise RuntimeError(
            "\\nУже запущен другой локальный экземпляр "
            "этого Telegram-бота.\\n"
            "Закройте старое окно Kaspi Profit Bot и "
            "запустите программу ещё раз.\\n"
            f"Local lock port: {port}"
        ) from exc

    _INSTANCE_SOCKET = sock
    print(
        f"Local single-instance lock: 127.0.0.1:{port}"
    )


def build_app():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN не заполнен в .env"
        )

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(8)
        .build()
    )

    app.add_error_handler(
        telegram_error_handler
    )

    app.add_handler(
        CommandHandler("start", cmd_start)
    )
    app.add_handler(
        CommandHandler("help", cmd_start)
    )
    app.add_handler(
        CommandHandler("shops", cmd_shops)
    )
    app.add_handler(
        CommandHandler("today", cmd_today)
    )
    app.add_handler(
        CommandHandler("yesterday", cmd_yesterday)
    )
    app.add_handler(
        CommandHandler("week", cmd_week)
    )
    app.add_handler(
        CommandHandler("settings", cmd_settings)
    )
    app.add_handler(
        CommandHandler("date", cmd_date)
    )
    app.add_handler(
        CommandHandler("period", cmd_period)
    )
    app.add_handler(
        CommandHandler("sync", cmd_sync)
    )
    app.add_handler(
        CommandHandler("apitest", cmd_apitest)
    )
    app.add_handler(
        CommandHandler("deliverydebug", cmd_deliverydebug)
    )
    app.add_handler(
        CommandHandler("phototest", cmd_phototest)
    )
    app.add_handler(
        CommandHandler("costs", cmd_costs)
    )
    app.add_handler(
        CommandHandler("marketing", cmd_marketing)
    )
    app.add_handler(
        CommandHandler("returns", cmd_returns)
    )
    app.add_handler(
        CommandHandler("missing", cmd_missing)
    )
    app.add_handler(
        CommandHandler("status", cmd_status)
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_settings,
            pattern=r"^settings:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_shops,
            pattern=r"^(shop|shops)",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_calendar,
            pattern=r"^(cal|datepick|periodstart|periodend)",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_day_detail,
            pattern=r"^daydetail:",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            callback_returns,
            pattern=r"^returns:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_cost_date,
            pattern=r"^costdate:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_marketing_cabinet,
            pattern=r"^marketcab:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_marketing_report,
            pattern=r"^marketreport:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_marketing_campaign,
            pattern=r"^marketcampaign:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_marketing,
            pattern=r"^marketing:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_cost_edit,
            pattern=r"^costedit",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_financial_settings,
            pattern=r"^fees:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_cost_settings,
            pattern=r"^costsettings:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_costs,
            pattern=r"^(missing|costopen|costgroup:)",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_document,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    if app.job_queue:
        app.job_queue.run_repeating(
            scheduled_sync,
            interval=SYNC_INTERVAL_MINUTES * 60,
            first=20,
            name="kaspi_all_shops_sync",
        )

        app.job_queue.run_repeating(
            scheduled_marketing_sync,
            interval=MARKETING_SYNC_INTERVAL_MINUTES * 60,
            first=45,
            name="kaspi_marketing_all_shops_sync",
        )

    return app


if __name__ == "__main__":
    print("Kaspi Profit Bot v4.19.1 — Safe model/category cost grouping")
    print("Database:", DB_PATH)
    print("Secret key:", KEY_PATH)
    print("Timezone:", TIMEZONE)
    print("Public access:", PUBLIC_ACCESS)
    print("Max shops per user:", MAX_SHOPS_PER_USER)

    acquire_local_instance_lock()

    # Python 3.14 no longer creates a default asyncio event loop
    # for the main thread. python-telegram-bot run_polling() expects one.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    build_app().run_polling(
        drop_pending_updates=False,
        bootstrap_retries=TELEGRAM_BOOTSTRAP_RETRIES,
    )
