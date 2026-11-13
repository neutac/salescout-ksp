"""SQLite: запросы по наименованию, карточки и снимки только при изменении."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kaspi_auth import hash_password, new_session_token, token_hash, verify_password

QYZYLORDA = timezone(timedelta(hours=5))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queries (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city_id TEXT NOT NULL,
    checks_planned INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL,
    checks_done INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stopped_at TEXT,
    next_run_at TEXT,
    last_error TEXT,
    progress TEXT,
    search_total INTEGER,
    last_thousand_ms INTEGER
);
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    query_id INTEGER NOT NULL REFERENCES queries(id),
    product_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    brand TEXT,
    category TEXT,
    image TEXT,
    price REAL,
    rating REAL,
    listing_reviews INTEGER,
    updated_at TEXT NOT NULL,
    UNIQUE(query_id, product_id)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES cards(id),
    query_id INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    stock_total INTEGER,
    review_count INTEGER,
    stars_json TEXT NOT NULL,
    merchants_json TEXT NOT NULL,
    signature TEXT NOT NULL,
    change_note TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(QYZYLORDA).isoformat(timespec="seconds")


def review_outcome(stars: dict[str, Any] | None) -> dict[str, int]:
    """2, 3, 4 и 5 звёзд — покупка. 1 звезда — возврат.

    Один такой отзыв считается одной покупкой или одним возвратом.
    """
    counts: dict[str, int] = {}
    for mark in ("1", "2", "3", "4", "5"):
        raw = (stars or {}).get(mark, 0)
        counts[mark] = raw if isinstance(raw, int) and raw >= 0 else 0
    purchases = counts["2"] + counts["3"] + counts["4"] + counts["5"]
    returns = counts["1"]
    return {
        "1": counts["1"],
        "2": counts["2"],
        "3": counts["3"],
        "4": counts["4"],
        "5": counts["5"],
        "bad": returns,
        "good": purchases,
        "neutral": 0,
        "purchases": purchases,
        "returns": returns,
    }


def observation_signature(snapshot: dict[str, Any]) -> str:
    merchants = []
    for merchant in snapshot.get("merchants") or []:
        merchants.append(
            {
                "id": merchant.get("merchant_id"),
                "price": merchant.get("price"),
                "stock": merchant.get("stock"),
                "delivery": [item.get("label") for item in merchant.get("delivery") or []],
            }
        )
    merchants.sort(key=lambda item: str(item["id"]))
    payload = {
        "stock_total": snapshot.get("stock_total"),
        "review_count": snapshot.get("review_count"),
        "stars": snapshot.get("stars") or {},
        "merchants": merchants,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def describe_change(previous: dict[str, Any] | None, snapshot: dict[str, Any]) -> str:
    if previous is None:
        return "первое чтение"
    parts: list[str] = []
    if previous.get("stock_total") != snapshot.get("stock_total"):
        parts.append(f"склад {previous.get('stock_total')} → {snapshot.get('stock_total')}")
    if previous.get("review_count") != snapshot.get("review_count"):
        parts.append(f"отзывы {previous.get('review_count')} → {snapshot.get('review_count')}")
    prev_stars = previous.get("stars") or {}
    new_stars = snapshot.get("stars") or {}
    if prev_stars != new_stars:
        parts.append("изменилось число оценок по звёздам")
    if not parts:
        parts.append("изменились продавцы, цена или срок доставки")
    return ", ".join(parts)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(queries)")}
        if "user_id" not in columns:
            conn.execute("ALTER TABLE queries ADD COLUMN user_id INTEGER REFERENCES users(id)")
        if "last_thousand_ms" not in columns:
            conn.execute("ALTER TABLE queries ADD COLUMN last_thousand_ms INTEGER")
        card_columns = {row[1] for row in conn.execute("PRAGMA table_info(cards)")}
        if "cities" not in card_columns:
            conn.execute("ALTER TABLE cards ADD COLUMN cities TEXT")
        if "stock_city" not in card_columns:
            conn.execute("ALTER TABLE cards ADD COLUMN stock_city TEXT")

    def _public_user(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "username": row["username"],
            "role": row["role"],
            "disabled": bool(row["disabled"]),
            "created_at": row["created_at"],
        }

    def ensure_admin(self, username: str, password: str) -> dict[str, Any]:
        salt, digest = hash_password(password)
        moment = now_iso()
        with self._lock, self._connect() as conn:
            existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO users (username, password_salt, password_hash, role, disabled, created_at)
                    VALUES (?, ?, ?, 'admin', 0, ?)
                    """,
                    (username, salt, digest, moment),
                )
                user_id = int(cursor.lastrowid)
            else:
                user_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE users
                    SET password_salt = ?, password_hash = ?, role = 'admin', disabled = 0
                    WHERE id = ?
                    """,
                    (salt, digest, user_id),
                )
            conn.execute("UPDATE queries SET user_id = ? WHERE user_id IS NULL", (user_id,))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._public_user(row)

    def create_user(self, username: str, password: str, role: str) -> dict[str, Any]:
        salt, digest = hash_password(password)
        with self._lock, self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO users (username, password_salt, password_hash, role, disabled, created_at)
                    VALUES (?, ?, ?, ?, 0, ?)
                    """,
                    (username, salt, digest, role, now_iso()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Такой пользователь уже есть") from exc
            row = conn.execute("SELECT * FROM users WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        return self._public_user(row)

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [self._public_user(row) for row in rows]

    def get_user(self, user_id: int) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise KeyError(user_id)
        return self._public_user(row)

    def update_user(self, user_id: int, *, disabled: bool | None = None, password: str | None = None) -> dict[str, Any]:
        assignments: list[str] = []
        values: list[Any] = []
        if disabled is not None:
            assignments.append("disabled = ?")
            values.append(1 if disabled else 0)
        if password is not None:
            salt, digest = hash_password(password)
            assignments.extend(["password_salt = ?", "password_hash = ?"])
            values.extend([salt, digest])
        if not assignments:
            return self.get_user(user_id)
        values.append(user_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE users SET {', '.join(assignments)} WHERE id = ?", values)
            if disabled:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise KeyError(user_id)
        return self._public_user(row)

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None or row["disabled"]:
            return None
        if not verify_password(password, row["password_salt"], row["password_hash"]):
            return None
        return self._public_user(row)

    def open_session(self, user_id: int) -> str:
        raw, digest = new_session_token()
        expires = (datetime.now(QYZYLORDA) + timedelta(days=14)).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                (user_id, digest, expires),
            )
        return raw

    def user_by_token(self, raw: str | None) -> dict[str, Any] | None:
        if not raw:
            return None
        digest = token_hash(raw)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT users.*
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.disabled = 0
                """,
                (digest, now_iso()),
            ).fetchone()
        return self._public_user(row) if row else None

    def close_session(self, raw: str | None) -> None:
        if not raw:
            return
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(raw),))

    def active_query_count(self, user_id: int) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM queries
                WHERE user_id = ? AND status IN ('queued', 'running', 'waiting')
                """,
                (user_id,),
            ).fetchone()
        return int(row["n"])

    def recent_query_count(self, user_id: int, since: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM queries WHERE user_id = ? AND created_at >= ?",
                (user_id, since),
            ).fetchone()
        return int(row["n"])

    def active_query_total(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM queries WHERE status IN ('queued', 'running', 'waiting')"
            ).fetchone()
        return int(row["n"])

    def recent_query_total(self, since: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM queries WHERE created_at >= ?",
                (since,),
            ).fetchone()
        return int(row["n"])

    def create_query(self, name: str, city_id: str, checks_planned: int, interval_seconds: int, user_id: int) -> dict[str, Any]:
        moment = now_iso()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO queries (
                    name, city_id, checks_planned, interval_seconds, checks_done, status,
                    created_at, updated_at, progress, user_id
                ) VALUES (?, ?, ?, ?, 0, 'queued', ?, ?, ?, ?)
                """,
                (name, city_id, checks_planned, interval_seconds, moment, moment, "в очереди", user_id),
            )
            query_id = int(cursor.lastrowid)
        return self.get_query(query_id)

    def get_query(self, query_id: int) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM queries WHERE id = ?", (query_id,)).fetchone()
        if row is None:
            raise KeyError(query_id)
        return dict(row)

    def list_queries(self, user_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if user_id is None:
                rows = conn.execute(
                    """
                    SELECT queries.*, users.username AS owner_name
                    FROM queries
                    LEFT JOIN users ON users.id = queries.user_id
                    ORDER BY queries.id DESC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT queries.*, users.username AS owner_name
                    FROM queries
                    LEFT JOIN users ON users.id = queries.user_id
                    WHERE queries.user_id = ?
                    ORDER BY queries.id DESC
                    """,
                    (user_id,),
                ).fetchall()
            counts = {
                int(row["query_id"]): int(row["n"])
                for row in conn.execute("SELECT query_id, COUNT(*) AS n FROM cards GROUP BY query_id")
            }
        result = []
        for row in rows:
            item = dict(row)
            item["cards"] = counts.get(int(item["id"]), 0)
            result.append(item)
        return result

    def update_query(self, query_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [query_id]
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE queries SET {columns} WHERE id = ?", values)

    def increment_check(self, query_id: int) -> int:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE queries SET checks_done = checks_done + 1, updated_at = ? WHERE id = ?",
                (now_iso(), query_id),
            )
            row = conn.execute("SELECT checks_done FROM queries WHERE id = ?", (query_id,)).fetchone()
        return int(row["checks_done"])

    def upsert_card(self, query_id: int, card: dict[str, Any]) -> int:
        moment = now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cards (
                    query_id, product_id, title, url, brand, category, image, price, rating,
                    listing_reviews, cities, stock_city, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(query_id, product_id) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    brand = excluded.brand,
                    category = excluded.category,
                    image = excluded.image,
                    price = excluded.price,
                    rating = excluded.rating,
                    listing_reviews = excluded.listing_reviews,
                    cities = excluded.cities,
                    stock_city = excluded.stock_city,
                    updated_at = excluded.updated_at
                """,
                (
                    query_id,
                    card["product_id"],
                    card["title"],
                    card.get("url"),
                    card.get("brand"),
                    card.get("category"),
                    card.get("image"),
                    card.get("price"),
                    card.get("rating"),
                    card.get("listing_reviews"),
                    card.get("cities"),
                    card.get("stock_city"),
                    moment,
                ),
            )
            row = conn.execute(
                "SELECT id FROM cards WHERE query_id = ? AND product_id = ?",
                (query_id, card["product_id"]),
            ).fetchone()
        return int(row["id"])

    def latest_observation(self, card_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM observations WHERE card_id = ? ORDER BY id DESC LIMIT 1",
                (card_id,),
            ).fetchone()
        return _observation(row) if row else None

    def save_if_changed(self, card_id: int, query_id: int, snapshot: dict[str, Any]) -> bool:
        signature = observation_signature(snapshot)
        previous = self.latest_observation(card_id)
        if previous is not None and previous["signature"] == signature:
            return False
        note = describe_change(previous, snapshot)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO observations (
                    card_id, query_id, observed_at, stock_total, review_count, stars_json,
                    merchants_json, signature, change_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    card_id,
                    query_id,
                    now_iso(),
                    snapshot.get("stock_total"),
                    snapshot.get("review_count"),
                    json.dumps(snapshot.get("stars") or {}, ensure_ascii=False),
                    json.dumps(snapshot.get("merchants") or [], ensure_ascii=False),
                    signature,
                    note,
                ),
            )
        return True

    def detail(self, query_id: int) -> dict[str, Any]:
        query = self.get_query(query_id)
        with self._lock, self._connect() as conn:
            cards = conn.execute(
                "SELECT * FROM cards WHERE query_id = ? ORDER BY price IS NULL, price, title",
                (query_id,),
            ).fetchall()
            observations = conn.execute(
                "SELECT * FROM observations WHERE query_id = ? ORDER BY id",
                (query_id,),
            ).fetchall()
        by_card: dict[int, list[dict[str, Any]]] = {}
        for row in observations:
            by_card.setdefault(int(row["card_id"]), []).append(_observation(row))
        card_views = []
        for row in cards:
            history = by_card.get(int(row["id"]), [])
            card_views.append({**dict(row), "latest": history[-1] if history else None, "history": history})
        return {
            "query": {**query, "cards": len(card_views)},
            "cards": card_views,
            "suppliers": _suppliers(card_views),
            "delivery_groups": _delivery_groups(card_views),
            "overview": _overview(card_views, query.get("search_total")),
        }


def _observation(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["stars"] = json.loads(item.pop("stars_json") or "{}")
    item["merchants"] = json.loads(item.pop("merchants_json") or "[]")
    item["outcome"] = review_outcome(item["stars"])
    return item


def _overview(cards: list[dict[str, Any]], search_total: int | None) -> dict[str, Any]:
    prices = [card["price"] for card in cards if isinstance(card.get("price"), (int, float))]
    review_sum = 0
    review_known = 0
    purchase_sum = 0
    return_sum = 0
    outcome_known = False
    stock_sum = 0
    stock_known = 0
    sellers: set[str] = set()
    for card in cards:
        latest = card.get("latest") or {}
        if latest.get("outcome"):
            purchase_sum += int(latest["outcome"].get("purchases") or 0)
            return_sum += int(latest["outcome"].get("returns") or 0)
            outcome_known = True
        if isinstance(latest.get("review_count"), int):
            review_sum += latest["review_count"]
            review_known += 1
        elif isinstance(card.get("listing_reviews"), int):
            review_sum += card["listing_reviews"]
            review_known += 1
        if isinstance(latest.get("stock_total"), int):
            stock_sum += latest["stock_total"]
            stock_known += 1
        for merchant in latest.get("merchants") or []:
            if merchant.get("merchant_id"):
                sellers.add(str(merchant["merchant_id"]))
    return {
        "cards": len(cards),
        "search_total": search_total,
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "review_sum": review_sum if review_known else None,
        "purchases": purchase_sum if outcome_known else None,
        "returns": return_sum if outcome_known else None,
        "stock_sum": stock_sum if stock_known else None,
        "sellers": len(sellers),
    }


def _suppliers(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for card in cards:
        latest = card.get("latest") or {}
        reviews = latest.get("review_count")
        if not isinstance(reviews, int):
            reviews = card.get("listing_reviews")
        for merchant in latest.get("merchants") or []:
            merchant_id = str(merchant.get("merchant_id") or "")
            if not merchant_id:
                continue
            row = grouped.setdefault(
                merchant_id,
                {
                    "merchant_id": merchant_id,
                    "merchant_name": merchant.get("merchant_name") or merchant_id,
                    "cards": [],
                    "stock": 0,
                    "stock_known": False,
                    "prices": [],
                    "deliveries": [],
                    "card_reviews": 0,
                    "reviews_known": False,
                },
            )
            if merchant.get("merchant_name"):
                row["merchant_name"] = merchant["merchant_name"]
            row["cards"].append({"product_id": card["product_id"], "title": card["title"]})
            if isinstance(merchant.get("stock"), int):
                row["stock"] += merchant["stock"]
                row["stock_known"] = True
            if isinstance(merchant.get("price"), (int, float)):
                row["prices"].append(merchant["price"])
            for delivery in merchant.get("delivery") or []:
                label = delivery.get("label")
                if label and label not in row["deliveries"]:
                    row["deliveries"].append(label)
            if isinstance(reviews, int):
                row["card_reviews"] += reviews
                row["reviews_known"] = True
    rows = []
    for row in grouped.values():
        prices = row.pop("prices")
        stock_known = row.pop("stock_known")
        reviews_known = row.pop("reviews_known")
        row["stock"] = row["stock"] if stock_known else None
        row["card_reviews"] = row["card_reviews"] if reviews_known else None
        row["price_min"] = min(prices) if prices else None
        row["price_max"] = max(prices) if prices else None
        row["card_count"] = len(row["cards"])
        rows.append(row)
    rows.sort(key=lambda item: (-item["card_count"], item["merchant_name"]))
    return rows


def _delivery_groups(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        latest = card.get("latest") or {}
        merchants = latest.get("merchants") or []
        if not merchants:
            groups.setdefault("срок ещё не прочитан", []).append(
                {"title": card["title"], "product_id": card["product_id"], "merchant_name": "—", "stock": None, "price": card.get("price")}
            )
            continue
        for merchant in merchants:
            labels = [item.get("label") for item in merchant.get("delivery") or [] if item.get("label")]
            label = labels[0] if labels else "срок не указан"
            groups.setdefault(label, []).append(
                {
                    "title": card["title"],
                    "product_id": card["product_id"],
                    "merchant_name": merchant.get("merchant_name") or merchant.get("merchant_id"),
                    "stock": merchant.get("stock"),
                    "price": merchant.get("price"),
                }
            )
    return [{"label": label, "rows": rows} for label, rows in groups.items()]
