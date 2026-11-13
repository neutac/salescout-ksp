"""Поиск карточек Kaspi по наименованию сразу по всем городам магазина.

Города и коды взяты из списка Kaspi для запросов магазина.
Один проход читает первую страницу выдачи в каждом городе и склеивает карточки.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from kaspi_card import Client

MAX_PAGES = 1
PAGE_SIZE_HINT = 12
# Коды городов: https://guide.kaspi.kz/partner/ru/shop/api/general/q3200
CITIES: tuple[tuple[str, str], ...] = (
    ("471010000", "Актау"),
    ("151010000", "Актобе"),
    ("750000000", "Алматы"),
    ("710000000", "Астана"),
    ("231010000", "Атырау"),
    ("351810000", "Жезказган"),
    ("351010000", "Караганда"),
    ("111010000", "Кокшетау"),
    ("191610000", "Конаев"),
    ("391010000", "Костанай"),
    ("431010000", "Кызылорда"),
    ("551010000", "Павлодар"),
    ("591010000", "Петропавловск"),
    ("632810000", "Семей"),
    ("191010000", "Талдыкорган"),
    ("311010000", "Тараз"),
    ("512610000", "Туркестан"),
    ("271010000", "Уральск"),
    ("631010000", "Усть-Каменогорск"),
    ("511010000", "Шымкент"),
)


def _cards_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("cards"), list):
        return [card for card in data["cards"] if isinstance(card, dict)]
    if isinstance(payload.get("cards"), list):
        return [card for card in payload["cards"] if isinstance(card, dict)]
    return []


def _reported_total(payload: Any, fallback: int) -> int | None:
    if not isinstance(payload, dict):
        return fallback
    containers = [payload]
    if isinstance(payload.get("data"), dict):
        containers.append(payload["data"])
    for container in containers:
        for key in ("total", "totalCount", "cardsCount", "itemsCount"):
            value = container.get(key)
            if isinstance(value, int):
                return value
    return fallback


def _numeric_id(card: dict[str, Any]) -> str | None:
    raw = card.get("id")
    if raw is not None:
        match = re.match(r"^(\d+)", str(raw))
        if match:
            return match.group(1)
    link = str(card.get("shopLink") or "")
    match = re.search(r"-(\d+)[a-zA-Z]*/?(?:\?|$)", link)
    return match.group(1) if match else None


def _image(card: dict[str, Any]) -> str | None:
    for image in card.get("previewImages") or []:
        if not isinstance(image, dict):
            continue
        url = image.get("medium") or image.get("large") or image.get("small")
        if isinstance(url, str) and url:
            return url
    return None


def _link(shop_link: Any, city: str) -> str | None:
    if not isinstance(shop_link, str) or not shop_link:
        return None
    if shop_link.startswith("https://kaspi.kz/"):
        url = shop_link
    elif shop_link.startswith("/"):
        path = "/shop" + shop_link if shop_link.startswith("/p/") else shop_link
        url = "https://kaspi.kz" + path
    else:
        return None
    if "c=" not in url:
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}c={city}"
    return url


def _category(card: dict[str, Any]) -> str | None:
    raw = card.get("categoryRu") or card.get("category")
    if isinstance(raw, list):
        parts = [item for item in raw if isinstance(item, str)]
        return " / ".join(parts) if parts else None
    if isinstance(raw, str):
        return raw
    return None


def normalize_card(card: dict[str, Any], city: str) -> dict[str, Any] | None:
    product_id = _numeric_id(card)
    title = card.get("title")
    if not product_id or not isinstance(title, str) or not title.strip():
        return None
    price = card.get("unitSalePrice")
    if price is None:
        price = card.get("unitPrice")
    category = _category(card)
    return {
        "product_id": product_id,
        "title": title.strip(),
        "url": _link(card.get("shopLink"), city),
        "brand": card.get("brand") if isinstance(card.get("brand"), str) else None,
        "category": category,
        "image": _image(card),
        "price": price if isinstance(price, (int, float)) else None,
        "rating": card.get("rating") if isinstance(card.get("rating"), (int, float)) else None,
        "listing_reviews": card.get("reviewsQuantity") if isinstance(card.get("reviewsQuantity"), int) else None,
    }


def search_cards(client: Client, text: str, city: str | None = None, pages: int = MAX_PAGES) -> dict[str, Any]:
    """Первая страница выдачи в каждом городе. Покупку не оформляет.

    `city` оставлен для старых вызовов и не сужает поиск до одного города.
    """
    del city
    found: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    total = 0
    refused: list[str] = []
    saw_limit = False
    limit = pages * PAGE_SIZE_HINT
    for city_id, city_name in CITIES:
        referer = (
            "https://kaspi.kz/shop/search/?text="
            + quote(text)
            + "&sort=relevance&filteredByCategory=false&sc="
        )
        status, payload, error, _attempts = client.request(
            "GET",
            "https://kaspi.kz/yml/product-view/pl/results",
            headers={
                "Accept": "application/json, text/*",
                "Origin": "https://kaspi.kz",
                "Referer": referer,
                "X-Requested-With": "XMLHttpRequest",
                "x-ks-city": city_id,
                "Cookie": f"kaspi.storefront.cookie.city={city_id}",
            },
            params={
                "text": text,
                "page": 0,
                "c": city_id,
                "sort": "relevance",
                "filteredByCategory": "false",
                "sc": "",
                "ui": "d",
            },
        )
        if status == 429:
            refused.append(city_name)
            saw_limit = True
            break
        if status != 200 or error:
            refused.append(city_name)
            continue
        batch = _cards_from_payload(payload)
        reported = _reported_total(payload, len(batch))
        if isinstance(reported, int):
            total = max(total, reported)
        for raw in batch:
            card = normalize_card(raw, city_id)
            if card is None:
                continue
            current = found.get(card["product_id"])
            if current is None:
                if len(order) >= limit:
                    continue
                card["cities"] = [city_name]
                card["stock_city"] = city_id
                found[card["product_id"]] = card
                order.append(card["product_id"])
                continue
            names: list[str] = current["cities"]
            if city_name not in names:
                names.append(city_name)
    cards = []
    for product_id in order:
        card = found[product_id]
        card["cities"] = ", ".join(card["cities"])
        cards.append(card)
    if cards:
        return {"cards": cards, "total": total or len(cards), "error": None}
    if saw_limit:
        return {
            "cards": [],
            "total": total or None,
            "error": "Kaspi ответил 429 и после паузы не открыл поиск. Обход ограничения не выполняется: тот же запрос просто ждёт и повторяется.",
        }
    if refused:
        return {"cards": [], "total": total or None, "error": "Поиск не открылся ни в одном городе."}
    return {"cards": [], "total": 0, "error": None}
