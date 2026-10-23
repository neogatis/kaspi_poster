"""Authenticated web editor for Kaspi Profit Bot's existing SQLite database.

Log in through /panel in a private Telegram chat (single-use link).
No credentials, Telegram tokens or merchant API secrets are exposed to the browser.
"""
from __future__ import annotations

import asyncio
import csv
import queue
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlparse
from contextlib import contextmanager

import main as bot
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

FILES = Path(__file__).resolve().parent
ORIGIN = os.getenv("WEB_PUBLIC_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
LINK_TTL = 10 * 60
SESSION_TTL = 7 * 24 * 60 * 60
MAX_BODY = 1024 * 1024
# Single worker. Kaspi is rate-limited; never fan out image requests.
PHOTO_QUEUE = queue.Queue(maxsize=200)
PHOTO_PENDING = set()
PHOTO_LOCK = threading.Lock()
PHOTO_DELAY_SECONDS = 5
PHOTO_NEGATIVE_TTL = 6 * 60 * 60
PHOTO_POSITIVE_TTL = 30 * 24 * 60 * 60


@contextmanager
def connect():
    conn = sqlite3.connect(bot.DB_PATH, timeout=15)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def database_view(conn):
    obj = bot.Database.__new__(bot.Database)
    obj.conn = conn
    return obj


def prepare_db():
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS web_login_links (
                token_hash TEXT PRIMARY KEY,
                telegram_user_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS web_sessions (
                token_hash TEXT PRIMARY KEY,
                telegram_user_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS web_photo_lookup (
                shop_id INTEGER NOT NULL,
                product_code TEXT NOT NULL,
                checked_at INTEGER NOT NULL,
                found INTEGER NOT NULL,
                PRIMARY KEY(shop_id, product_code)
            );
            CREATE TABLE IF NOT EXISTS web_product_stock (
                shop_id INTEGER NOT NULL,
                identity_key TEXT NOT NULL,
                quantity REAL NOT NULL DEFAULT 0 CHECK(quantity >= 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (shop_id, identity_key),
                FOREIGN KEY(shop_id) REFERENCES shops(id) ON DELETE CASCADE
            );
        """)


def digest(token):
    return hashlib.sha256(token.encode("utf8")).hexdigest()


def now():
    return int(time.time())


def fresh_login_link(telegram_user_id):
    token = secrets.token_urlsafe(36)
    with connect() as conn:
        conn.execute("DELETE FROM web_login_links WHERE expires_at < ?", (now(),))
        conn.execute(
            "INSERT INTO web_login_links (token_hash,telegram_user_id,expires_at) VALUES (?,?,?)",
            (digest(token), str(telegram_user_id), now() + LINK_TTL),
        )
    return ORIGIN + "/auth?code=" + token


async def cmd_panel(update, context):
    if not await bot.guard(update):
        return
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Откройте личный чат с ботом и отправьте /panel там.")
        return
    uid = str(update.effective_user.id)
    if not bot.DB.shops_for_user(uid):
        await update.effective_message.reply_text("Сначала подключите магазин через /shops.")
        return
    if not ORIGIN.startswith("https://"):
        await update.effective_message.reply_text("Адрес веб-панели ещё не настроен администратором.")
        return
    url = fresh_login_link(uid)
    await update.effective_message.reply_text(
        "📦 Веб-панель Ambrello\n\n"
        "Открывай список товаров, указывай себестоимость и остатки, массово редактируй цены. "
        "Данные сохраняются сразу в базе Kaspi Profit Bot.\n\n"
        "🔒 Ссылка одноразовая и действует 10 минут. Не пересылай её другим.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Открыть панель", url=url)]]),
    )


async def cmd_stock(update, context):
    if not await bot.guard(update):
        return
    shop = bot.DB.active_shop(update.effective_user.id)
    if not shop:
        await update.effective_message.reply_text("Сначала выберите магазин через /shops.")
        return
    with connect() as conn:
        summary = conn.execute(
            "SELECT COUNT(*) AS products, COALESCE(SUM(quantity),0) AS qty "
            "FROM web_product_stock WHERE shop_id=?", (shop["id"],),
        ).fetchone()
    await update.effective_message.reply_text(
        f"📦 Учтённые вручную остатки · {shop['name']}\n\n"
        f"Товарных позиций: {summary['products']}\n"
        f"Количество: {summary['qty']:g} шт.\n\n"
        "Чтобы изменить остатки или себестоимость, отправьте /panel.\n"
        "Обратите внимание: ручные остатки пока не списываются автоматически по заказам.",
    )


def stock_quantity(conn, shop_id, identity):
    r = conn.execute(
        "SELECT quantity FROM web_product_stock WHERE shop_id=? AND identity_key=?",
        (shop_id, identity),
    ).fetchone()
    return float(r["quantity"]) if r else None


def cached_photo(db, shop_id, item):
    """Reuse only exact product-code photos; never join on a shared seller SKU."""
    photo = bot.gallery_medium_url(item["image_url"])
    if photo:
        return photo
    code = str(item["kaspi_product_code"] or "").strip()
    if not code:
        return None
    hit = db.conn.execute(
        "SELECT image_url FROM order_items WHERE shop_id=? AND kaspi_product_code=? "
        "AND image_url IS NOT NULL AND LENGTH(image_url)>12 "
        "ORDER BY rowid DESC LIMIT 1", (shop_id, code)
    ).fetchone()
    return bot.gallery_medium_url(hit["image_url"]) if hit else None


def photo_worker():
    while True:
        shop, code = PHOTO_QUEUE.get()
        try:
            # Import + invocation confined to background worker; HTTP never blocks web UI.
            image = asyncio.run(bot.resolve_public_product_image(code))
            image = bot.gallery_medium_url(image)
            with connect() as conn:
                if image:
                    conn.execute(
                        "UPDATE order_items SET image_url=? "
                        "WHERE shop_id=? AND kaspi_product_code=?",
                        (image, shop, code),
                    )
                conn.execute(
                    "INSERT INTO web_photo_lookup(shop_id,product_code,checked_at,found) VALUES (?,?,?,?) "
                    "ON CONFLICT(shop_id,product_code) DO UPDATE SET "
                    "checked_at=excluded.checked_at, found=excluded.found",
                    (shop, code, now(), 1 if image else 0),
                )
        except Exception as exc:
            print("Photo lookup failed:", type(exc).__name__, flush=True)
        finally:
            with PHOTO_LOCK:
                PHOTO_PENDING.discard((shop, code))
            PHOTO_QUEUE.task_done()
        # Slow down external lookups to avoid Kaspi HTTP 429.
        time.sleep(PHOTO_DELAY_SECONDS)


def schedule_photos(conn, shop, row_ids):
    if not isinstance(row_ids, list) or len(row_ids)>28:
        raise ValueError("Можно проверить не более 28 товаров за раз")
    clean = []
    for rid in row_ids:
        if isinstance(rid, bool):
            continue
        try: clean.append(int(rid))
        except (TypeError, ValueError): continue
    clean = sorted(set(r for r in clean if r>0))
    if not clean:
        return 0
    marks = ",".join("?" for _ in clean)
    rows = conn.execute(
        f"SELECT rowid AS rid,kaspi_product_code,image_url FROM order_items "
        f"WHERE shop_id=? AND rowid IN ({marks})", [shop, *clean]
    ).fetchall()
    queued=0
    for row in rows:
        code = str(row["kaspi_product_code"] or "").strip()
        if not code or cached_photo(database_view(conn), shop, row):
            continue
        prior = conn.execute(
            "SELECT checked_at,found FROM web_photo_lookup WHERE shop_id=? AND product_code=?", (shop,code)
        ).fetchone()
        if prior and now() - int(prior["checked_at"]) < (PHOTO_POSITIVE_TTL if prior["found"] else PHOTO_NEGATIVE_TTL):
            continue
        key=(shop,code)
        with PHOTO_LOCK:
            if key in PHOTO_PENDING:
                continue
            try:
                PHOTO_QUEUE.put_nowait(key)
            except queue.Full:
                break
            PHOTO_PENDING.add(key)
            queued+=1
    return queued


def make_products(db, shop_id):
    result = []
    for item in db.catalog_products(shop_id):
        origin = db.conn.execute(
            "SELECT sku,name,category,kaspi_product_code,master_product_id,image_url FROM order_items "
            "WHERE shop_id=? AND rowid=?", (shop_id, item["item_rowid"]),
        ).fetchone()
        if origin is None:
            continue
        key = bot.product_cost_identity(origin)
        group_key = bot.model_cost_group_key(origin["name"], origin["category"])
        result.append({
            "row_id": item["item_rowid"],
            "identity": key,
            "group": group_key,
            "sku": item["sku"],
            "name": item["name"],
            "category": bot.cost_product_type(item["name"], item["category"]),
            "photo": cached_photo(db, shop_id, origin),
            "sales_price": item["sale_price"],
            "cost": item["cost_price"],
            "stock": stock_quantity(db.conn, shop_id, key),
            "orders": item["order_count"],
            "model_eligible": bool(group_key),
        })
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "KaspiPanel/1.0"

    def log_message(self, format, *args):
        # Never print magic-link tokens or cookies into public deployment logs.
        return

    def send(self, code, payload=b"", content_type="application/json; charset=utf-8", extra=None):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf8")
        elif isinstance(payload, str):
            payload = payload.encode("utf8")
        self.send_response(int(code))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location, cookie=None):
        self.send(HTTPStatus.SEE_OTHER, b"", extra={"Location": location, **({"Set-Cookie": cookie} if cookie else {})})

    def current_user(self):
        raw = self.headers.get("Cookie", "")
        try:
            parsed = SimpleCookie(); parsed.load(raw)
            session = parsed.get("ks_session")
            if not session:
                return None
            key = digest(session.value)
            with connect() as conn:
                row = conn.execute(
                    "SELECT telegram_user_id FROM web_sessions WHERE token_hash=? AND expires_at>?",
                    (key, now()),
                ).fetchone()
            return str(row["telegram_user_id"]) if row else None
        except (ValueError, sqlite3.DatabaseError):
            return None

    def do_GET(self):
        try:
            self.handle_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print("Web GET error:", type(exc).__name__)
            try: self.send(500, {"error": "Внутренняя ошибка панели"})
            except OSError: pass

    def handle_get(self):
        route = urlsplit(self.path)
        path = route.path
        if path == "/health":
            return self.send(200, {"ok": True})
        if path == "/auth":
            code = parse_qs(route.query).get("code", [""])[0]
            if not code or len(code) > 150:
                return self.redirect("/login?error=invalid")
            session = secrets.token_urlsafe(42)
            with connect() as conn:
                record = conn.execute(
                    "SELECT telegram_user_id FROM web_login_links WHERE token_hash=? AND expires_at>?",
                    (digest(code), now()),
                ).fetchone()
                # Atomic one-time consumption under a SQLite write lock.
                conn.execute("BEGIN IMMEDIATE")
                deleted = conn.execute(
                    "DELETE FROM web_login_links WHERE token_hash=? AND expires_at>?",
                    (digest(code), now()),
                ).rowcount
                if not record or deleted != 1:
                    return self.redirect("/login?error=expired")
                conn.execute(
                    "INSERT INTO web_sessions(token_hash,telegram_user_id,expires_at) VALUES(?,?,?)",
                    (digest(session), str(record["telegram_user_id"]), now()+SESSION_TTL),
                )
            cookie = f"ks_session={session}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={SESSION_TTL}"
            return self.redirect("/", cookie=cookie)

        if path == "/login":
            page = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход · Ambrello</title><link rel="stylesheet" href="/panel.css"><body><main class="login-card"><span class="mark">A</span><h1>Ambrello</h1><p>Панель себестоимости и остатков Kaspi Profit Bot</p><div class="notice">Войдите через личный чат с вашим Telegram-ботом: отправьте <b>/panel</b> и нажмите «Открыть панель».</div><p class="muted">Ссылка одноразовая и действует 10 минут.</p></main></body></html>"""
            return self.send(200, page, "text/html; charset=utf-8")
        user = self.current_user()
        if not user:
            if path == "/panel.css":
                return self.send(200, (FILES / "panel.css").read_bytes(), "text/css; charset=utf-8")
            if path == "/":
                return self.redirect("/login")
            return self.send(401, {"error": "Авторизация истекла. Выполните /panel в Telegram"})
        if path in ("/", "/panel.css", "/panel.js"):
            fname = "panel.html" if path == "/" else path.lstrip("/")
            mimetype = "text/html" if fname.endswith("html") else "text/css" if fname.endswith("css") else "text/javascript"
            return self.send(200, (FILES / fname).read_bytes(), mimetype + "; charset=utf-8")
        if path == "/api/photos/status":
            args = parse_qs(route.query)
            shop = self.require_shop(user, args.get("shop", [""])[0])
            if shop is None: return
            try:
                ids = [int(x) for x in args.get("ids", [""])[0].split(",") if x]
            except ValueError:
                return self.send(400, {"error": "Неверный список товаров"})
            if len(ids)>28: return self.send(400, {"error": "Слишком много товаров"})
            with connect() as conn:
                db = database_view(conn)
                photos = {}
                for rid in ids:
                    row = conn.execute(
                        "SELECT rowid AS rid,kaspi_product_code,image_url FROM order_items WHERE shop_id=? AND rowid=?", (shop,rid)
                    ).fetchone()
                    if row:
                        url = cached_photo(db, shop, row)
                        if url: photos[str(rid)] = url
            with PHOTO_LOCK:
                pending = sum(1 for j,_ in PHOTO_PENDING if j == shop)
            return self.send(200, {"photos":photos, "pending":pending})
        if path == "/api/shops":
            with connect() as conn:
                rows = conn.execute("SELECT id,name,is_active FROM shops WHERE telegram_user_id=? ORDER BY is_active DESC, name", (user,)).fetchall()
            return self.send(200, {"shops": [dict(r) for r in rows]})
        if path in ("/api/products", "/api/export.csv"):
            args = parse_qs(route.query)
            shop = self.require_shop(user, args.get("shop", [""])[0])
            if shop is None: return
            with connect() as conn:
                products = make_products(database_view(conn), shop)
            if path == "/api/products":
                totals = {"products": len(products), "missing_cost": sum(p["cost"] is None for p in products), "stock_filled": sum(p["stock"] is not None for p in products)}
                return self.send(200, {"products": products, "totals": totals})
            stream = io.StringIO(); writer = csv.writer(stream, delimiter=";")
            writer.writerow(["identity", "sku", "category", "name", "cost_per_unit", "stock_quantity"])
            for p in products:
                writer.writerow([p["identity"], p["sku"], p["category"], p["name"], "" if p["cost"] is None else p["cost"], "" if p["stock"] is None else p["stock"]])
            data = ("\ufeff" + stream.getvalue()).encode("utf8")
            return self.send(200, data, "text/csv; charset=utf-8", {"Content-Disposition": "attachment; filename=kaspi-costs-stocks.csv"})
        return self.send(404, {"error": "Не найдено"})

    def require_shop(self, user, value):
        try:
            shop = int(value)
        except (TypeError, ValueError):
            self.send(400, {"error": "Выберите магазин"}); return None
        with connect() as conn:
            row = conn.execute("SELECT id FROM shops WHERE id=? AND telegram_user_id=?", (shop, user)).fetchone()
        if row is None:
            self.send(403, {"error": "Нет доступа к магазину"}); return None
        return shop

    def post_data(self):
        origin = self.headers.get("Origin", "")
        if origin and origin != ORIGIN:
            raise ValueError("Некорректный источник запроса")
        if self.headers.get("X-Panel-Request") != "1":
            raise ValueError("Недостаточно подтверждения запроса")
        if self.headers.get("Content-Type", "").split(";")[0].lower() != "application/json":
            raise ValueError("Ожидается JSON")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_BODY:
            raise ValueError("Неверный размер запроса")
        return json.loads(self.rfile.read(length))

    def do_POST(self):
        try:
            user = self.current_user()
            if user is None:
                return self.send(401, {"error": "Авторизация истекла. Выполните /panel в Telegram"})
            data = self.post_data()
            route = urlsplit(self.path).path
            if route == "/api/logout":
                raw = SimpleCookie(); raw.load(self.headers.get("Cookie", ""))
                if raw.get("ks_session"):
                    with connect() as conn:
                        conn.execute("DELETE FROM web_sessions WHERE token_hash=?", (digest(raw["ks_session"].value),))
                return self.send(200, {"ok": True}, extra={"Set-Cookie": "ks_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0"})
            if route == "/api/photos/queue":
                shop = self.require_shop(user, data.get("shop"))
                if shop is None: return
                with connect() as conn:
                    queued = schedule_photos(conn, shop, data.get("row_ids"))
                return self.send(200, {"queued": queued})
            if route != "/api/save":
                return self.send(404, {"error": "Не найдено"})
            shop = self.require_shop(user, data.get("shop"))
            if shop is None: return
            items = data.get("items")
            if not isinstance(items, list) or not 1 <= len(items) <= 150:
                raise ValueError("Укажите от 1 до 150 товаров")
            with connect() as conn:
                db = database_view(conn)
                updated = 0
                for body in items:
                    if not isinstance(body, dict): raise ValueError("Некорректная строка товара")
                    identity = str(body.get("identity", ""))
                    if not identity or len(identity) > 1500: raise ValueError("Не указан идентификатор товара")
                    item = conn.execute(
                        "SELECT * FROM order_items WHERE rowid=? AND shop_id=?",
                        (int(body.get("row_id") or 0), shop),
                    ).fetchone()
                    if item is None or not hmac.compare_digest(identity.encode("utf8"), bot.product_cost_identity(item).encode("utf8")):
                        raise ValueError("Список изменился: обновите страницу и повторите")
                    if "cost" in body and body["cost"] is not None and str(body["cost"]).strip() != "":
                        cost = float(str(body["cost"]).replace(",", "."))
                        if not (0 <= cost <= 1e9) or not (cost < float('inf')):
                            raise ValueError("Себестоимость вне допустимого диапазона")
                        scope = str(body.get("scope", "product"))
                        if scope not in ("product", "model"): raise ValueError("Некорректная область действия цены")
                        if scope == "model" and not bot.model_cost_group_key(item["name"], item["category"]):
                            raise ValueError("Для этого названия нельзя безопасно определить модель")
                        db.set_cost_for_item(shop, item, cost, whole_model=(scope == "model"))
                    if "stock" in body and body["stock"] is not None and str(body["stock"]).strip() != "":
                        stock = float(str(body["stock"]).replace(",", "."))
                        if not (0 <= stock <= 10000000) or stock != stock:
                            raise ValueError("Некорректное количество")
                        conn.execute(
                            """INSERT INTO web_product_stock(shop_id,identity_key,quantity,updated_at) VALUES(?,?,?,?)
                               ON CONFLICT(shop_id,identity_key) DO UPDATE SET quantity=excluded.quantity,updated_at=excluded.updated_at""",
                            (shop, identity, stock, datetime.now(timezone.utc).isoformat()),
                        )
                    updated += 1
                conn.commit()
            return self.send(200, {"ok": True, "updated": updated})
        except (TypeError, ValueError, json.JSONDecodeError, OverflowError) as exc:
            return self.send(400, {"error": str(exc)[:240]})
        except (sqlite3.DatabaseError, OSError) as exc:
            print("Web POST error:", type(exc).__name__)
            return self.send(500, {"error": "Не удалось записать данные. Повторите попытку"})


def start_server():
    prepare_db()
    threading.Thread(target=photo_worker, daemon=True, name="kaspi-photo-worker").start()
    host = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    thread = threading.Thread(target=host.serve_forever, daemon=True, name="kaspi-web-panel")
    thread.start()
    print(f"Web panel serving on port {PORT}", flush=True)
    return host
