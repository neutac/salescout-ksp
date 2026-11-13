#!/usr/bin/env python3
"""Сбор публичных данных карточки Kaspi.kz в Results/*.json.

Запросы идут через локальный SOCKS5-прокси (kaspi_proxy.py) с таймаутом.
На ответ 429 скрипт ждёт и повторяет тот же запрос с теми же заголовками.
Заголовки не подменяются: так магазин отличает обычный просмотр карточки,
а смена заголовков не обходит ограничение частоты.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from kaspi_proxy import LocalConnectProxy


def _curl():
    from curl_cffi import requests as curl_requests

    return curl_requests

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "Results"
QYZYLORDA = timezone(timedelta(hours=5))
DEFAULT_CITY = "750000000"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

PRODUCT_ID_RE = re.compile(r"-(\d+)(?:/)?(?:\?|#|$)")


class CardError(RuntimeError):
    pass


def parse_card_url(raw: str, city_override: str | None) -> tuple[str, str, str]:
    url = raw.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0].rstrip(".")
    if host != "kaspi.kz" and not host.endswith(".kaspi.kz"):
        raise CardError(f"Ожидается ссылка на kaspi.kz, получено: {raw}")
    match = PRODUCT_ID_RE.search(parsed.path)
    if not match:
        raise CardError(f"В ссылке нет кода товара: {raw}")
    product_id = match.group(1)
    query = parse_qs(parsed.query)
    city = city_override or (query.get("c") or [DEFAULT_CITY])[0]
    canonical = f"https://kaspi.kz{parsed.path}"
    if not canonical.endswith("/"):
        canonical += "/"
    page_url = f"{canonical}?c={city}"
    return product_id, city, page_url


def _headers(page_url: str, city: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/*,*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Origin": "https://kaspi.kz",
        "Referer": page_url,
        "User-Agent": USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "x-ks-city": city,
    }


def _looks_like_json(response: Any) -> bool:
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" in content_type:
        return True
    text = response.text.lstrip()
    return text.startswith("{") or text.startswith("[")


class Client:
    def __init__(self, proxy_url: str | None, timeout: float, retries: int) -> None:
        self.proxy_url = proxy_url
        self.timeout = timeout
        self.retries = retries
        options: dict[str, Any] = {
            "impersonate": "chrome",
            "timeout": timeout,
            "trust_env": False,
        }
        if proxy_url:
            options["proxy"] = proxy_url
        self.session = _curl().Session(**options)

    def spawn(self) -> Client:
        """Отдельная сессия для параллельного запроса. Общая сессия так не делится."""
        return Client(self.proxy_url, self.timeout, self.retries)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[int, Any | None, str | None, int]:
        """Возвращает статус, тело, текст ошибки и число попыток.

        429 и сетевой таймаут повторяются с паузой. Заголовки между
        попытками не меняются.
        """
        delay = 2.0
        attempts = 0
        last_error: str | None = None
        status = 0
        while attempts <= self.retries:
            attempts += 1
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )
            except _curl().RequestsError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                status = 0
                if attempts > self.retries:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            status = response.status_code
            if status == 429 and attempts <= self.retries:
                last_error = "429 Too Many Requests"
                print(f"429, пауза {delay:.0f} с, тот же запрос: {url}", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if _looks_like_json(response):
                try:
                    return status, response.json(), None, attempts
                except json.JSONDecodeError:
                    return status, None, "Ответ помечен как JSON, но не разбирается", attempts
            if status >= 400:
                return status, None, f"HTTP {status}", attempts
            return status, {"raw": response.text}, None, attempts
        return status, None, last_error or "нет ответа", attempts


def _record(
    bucket: dict[str, Any],
    errors: list[dict[str, Any]],
    name: str,
    status: int,
    data: Any | None,
    error: str | None,
    attempts: int,
) -> None:
    bucket[name] = {"status": status, "attempts": attempts, "data": data}
    if error or status >= 400 or status == 0:
        errors.append({"endpoint": name, "status": status, "attempts": attempts, "detail": error or f"HTTP {status}"})


def _extract_object(html: str, marker: str) -> Any | None:
    start = html.find(marker)
    if start < 0:
        return None
    brace = html.find("{", start)
    if brace < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(brace, len(html)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[brace : index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_product_page(html: str) -> dict[str, Any]:
    item = _extract_object(html, "BACKEND.components.item")
    digital = _extract_object(html, "window.digitalData.product")
    card = item.get("card") if isinstance(item, dict) else None
    availability = None
    match = re.search(r'property="product:availability"\s+content="([^"]+)"', html)
    if match:
        availability = match.group(1)
    schema_availability = re.findall(r'"availability"\s*:\s*"([^"]+)"', html)
    page: dict[str, Any] = {"availability_meta": availability, "schema_availability": schema_availability}
    if isinstance(card, dict):
        page["card"] = {
            "id": card.get("id"),
            "title": card.get("title"),
            "price": card.get("price"),
            "discount": card.get("discount"),
            "unit_price_before_discount": card.get("unitPriceBeforeDiscount"),
            "max_quantity": card.get("maxQuantity"),
            "unit": card.get("unit"),
            "rating": card.get("rating"),
            "reviews_quantity": card.get("reviewsQuantity"),
        }
    if isinstance(item, dict):
        page["specifications"] = item.get("specifications")
        page["description"] = item.get("description")
        page["gallery_images"] = item.get("galleryImages")
        page["breadcrumbs"] = item.get("breadcrumbs")
        page["video_id"] = item.get("videoId")
    if isinstance(digital, dict):
        page["analytics_stock"] = digital.get("stock")
        page["brand"] = digital.get("brand")
        page["category"] = digital.get("category")
    return page


def _probe_quantities() -> list[int]:
    """50, 100, 500, 1000, 5000, ... пока число не превысит разумный потолок."""
    value = 50
    steps: list[int] = []
    while value <= 100_000:
        steps.append(value)
        value = value * 2 if str(value).startswith("5") else value * 5
    return steps


def _reported_stock(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    stock = payload.get("maxStockQuantity")
    if isinstance(stock, int):
        return stock
    delivery = payload.get("deliveryQuantity")
    if isinstance(delivery, dict):
        numbers = [item for item in delivery.values() if isinstance(item, int)]
        if numbers:
            return max(numbers)
    return None


def probe_merchant_stock(
    client: Client,
    headers: dict[str, str],
    product_id: str,
    merchant_id: str,
    unit: str,
    increment: int | float,
) -> dict[str, Any]:
    """Перебирает размер заказа, пока сервер не покажет остаток меньше запроса.

    Это проверка лимита /yml/cart/shop/max-quantity. Она не подтверждает
    покупку и не создаёт оплату.
    """
    attempts_log: list[dict[str, Any]] = []
    stock: int | None = None
    for quantity in _probe_quantities():
        status, data, error, attempts = client.request(
            "GET",
            "https://kaspi.kz/yml/cart/shop/max-quantity",
            headers=headers,
            params={
                "sku": product_id,
                "uid": merchant_id,
                "unit": unit,
                "increment": increment,
                "quantity": quantity,
            },
        )
        reported = _reported_stock(data)
        attempts_log.append(
            {
                "requested_quantity": quantity,
                "status": status,
                "attempts": attempts,
                "reported_stock": reported,
                "detail": error,
            }
        )
        if reported is None:
            break
        stock = reported
        if quantity > reported:
            break
        time.sleep(0.3)
    return {"merchant_id": merchant_id, "StockAvailable": stock, "probes": attempts_log}


def product_review_breakdown(client: Client, headers: dict[str, str], product_id: str, city: str) -> dict[str, Any]:
    """Отзывы карточки: сводка и число оценок 1–5 по каждому продавцу."""
    review_headers = {**headers, "Content-Type": "application/json", "Content-Language": "ru-RU"}
    status, summary, error, attempts = client.request(
        "POST",
        "https://review.kaspi.kz/shop/display/api/api/v2/review-summary/product",
        headers=review_headers,
        json_body={"productId": product_id, "categoryCode": None},
    )
    buckets: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    pages = 0
    for page in range(30):
        page_status, data, page_error, page_attempts = client.request(
            "POST",
            f"https://review.kaspi.kz/shop/display/api/api/v2/review/product/{product_id}",
            headers=review_headers,
            params={"page": page, "reviewsCount": 100, "filterType": 0},
            json_body={"CityId": city, "ZoneId": []},
        )
        pages += 1
        if page_status != 200 or not isinstance(data, list) or not data:
            if page == 0:
                error = page_error or error
                status = page_status
            break
        fresh = 0
        for item in data:
            if not isinstance(item, dict):
                continue
            review_id = str(item.get("id") or "")
            if review_id and review_id in seen:
                continue
            if review_id:
                seen.add(review_id)
                fresh += 1
            if str(item.get("productId") or "") != product_id:
                continue
            rating = None
            for field in item.get("ratingFields") or []:
                if isinstance(field, dict) and field.get("code") == "product":
                    rating = field.get("selectedRating")
            merchant_id = str(item.get("merchantId") or "unknown")
            row = buckets.setdefault(
                merchant_id,
                {
                    "merchant_id": merchant_id,
                    "merchant_name": item.get("merchantName") or "",
                    "total": 0,
                    "stars": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
                },
            )
            if item.get("merchantName"):
                row["merchant_name"] = item["merchantName"]
            if rating in (1, 2, 3, 4, 5):
                row["stars"][str(rating)] += 1
                row["total"] += 1
        if len(data) < 100 or fresh == 0:
            break
        time.sleep(0.2)
    rows = sorted(buckets.values(), key=lambda row: row["total"], reverse=True)
    return {
        "status": status,
        "attempts": attempts,
        "error": error,
        "pages": pages,
        "summary": summary,
        "by_merchant": rows,
    }


def merchants_from_offers(payload: Any) -> list[dict[str, Any]]:
    offers: list[Any] = []
    if isinstance(payload, dict):
        inner = payload.get("offers")
        if isinstance(inner, dict) and isinstance(inner.get("offers"), list):
            offers = inner["offers"]
        elif isinstance(inner, list):
            offers = inner
        elif isinstance(payload.get("data"), dict):
            return merchants_from_offers(payload["data"])
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        merchant_id = str(offer.get("merchantId") or offer.get("merchantUID") or "")
        if not merchant_id or merchant_id in seen:
            continue
        seen.add(merchant_id)
        found.append(
            {
                "merchant_id": merchant_id,
                "merchant_name": offer.get("merchantName"),
                "price": offer.get("price"),
            }
        )
    return found


def quantity_view(collected: dict[str, Any]) -> dict[str, Any]:
    """Короткий срез по лимиту заказа. Складского остатка в этих полях нет."""
    limits = (collected.get("limits") or {}).get("data") or {}
    product_limit = {}
    if isinstance(limits, dict):
        product_limit = (limits.get("productLimit") or {}).get(collected["product_id"]) or {}
    card = (product_limit.get("card") or {}) if isinstance(product_limit, dict) else {}
    page_card = ((collected.get("page") or {}).get("data") or {}).get("card") or {}
    page = (collected.get("page") or {}).get("data") or {}
    return {
        "max_quantity": card.get("maxQuantity", page_card.get("max_quantity")),
        "increment": (card.get("unit") or page_card.get("unit") or {}).get("increment"),
        "unit": (card.get("unit") or page_card.get("unit") or {}).get("countingLiteral"),
        "availability": page.get("availability_meta"),
        "schema_availability": page.get("schema_availability"),
        "analytics_stock": page.get("analytics_stock"),
        "StockAvailable": collected.get("StockAvailable"),
        "note": "max_quantity — лимит одного заказа. StockAvailable — остаток склада, который сервер вернул на проверке количества.",
    }


def collect(client: Client, product_id: str, city: str, page_url: str) -> dict[str, Any]:
    headers = _headers(page_url, city)
    endpoints: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []

    calls: list[tuple[str, str, str, dict[str, Any] | None, dict[str, Any] | None]] = [
        (
            "content",
            "GET",
            f"https://kaspi.kz/yml/content/item/product/{product_id}",
            {"c": city},
            None,
        ),
        (
            "limits",
            "GET",
            "https://kaspi.kz/yml/content/item/product/limits",
            {"productCodes": product_id},
            None,
        ),
        (
            "review_summary",
            "GET",
            f"https://kaspi.kz/yml/creview/rest/misc/product/{product_id}/summary",
            None,
            None,
        ),
        (
            "reviews",
            "GET",
            f"https://kaspi.kz/yml/review-view/api/v1/reviews/product/{product_id}",
            {"filter": "ALL", "sort": "DATE", "limit": 50, "withAgg": "true"},
            None,
        ),
        (
            "product_availability",
            "GET",
            "https://kaspi.kz/yml/product-view/availability",
            {"code": product_id, "c": city},
            None,
        ),
        (
            "offer_availability",
            "GET",
            "https://kaspi.kz/yml/offer-view/availability",
            {"code": product_id, "c": city},
            None,
        ),
    ]

    for name, method, url, params, body in calls:
        status, data, error, attempts = client.request(method, url, headers=headers, params=params, json_body=body)
        _record(endpoints, errors, name, status, data, error, attempts)
        time.sleep(0.4)

    offer_body = {
        "cityId": city,
        "id": product_id,
        "merchantUID": "",
        "limit": 30,
        "page": 0,
        "sortOption": "PRICE",
        "highRating": False,
        "searchText": None,
        "isExcellentMerchant": False,
    }
    content_data = (endpoints.get("content") or {}).get("data")
    if isinstance(content_data, dict):
        promo = (content_data.get("card") or {}).get("promoConditions") or {}
        offer_body["product"] = {
            "brand": promo.get("brand") or "",
            "categoryCodes": promo.get("categoryCodes") or [],
            "baseProductCodes": promo.get("baseProductCodes") or [],
            "groups": promo.get("groups"),
        }
    status, data, error, attempts = client.request(
        "POST",
        f"https://kaspi.kz/yml/offer-view/offers/{product_id}",
        headers={**headers, "Content-Type": "application/json; charset=UTF-8"},
        params={"id": product_id},
        json_body=offer_body,
    )
    if status == 405:
        get_status, get_data, get_error, get_attempts = client.request(
            "GET",
            f"https://kaspi.kz/yml/offer-view/offers/{product_id}",
            headers=headers,
            params={"id": product_id, "cityId": city, "limit": 30, "page": 0, "sortOption": "PRICE"},
        )
        status, data, attempts = get_status, get_data, attempts + get_attempts
        if get_status < 400 and not get_error:
            error = None
        else:
            error = f"POST отклонён с 405. Повтор GET: {get_error or 'HTTP ' + str(get_status)}"
    _record(endpoints, errors, "offers", status, data, error, attempts)
    time.sleep(0.4)

    status, data, error, attempts = client.request("GET", page_url, headers={**headers, "Accept": "text/html,application/xhtml+xml"})
    page_payload: Any | None = None
    if status == 200 and isinstance(data, dict) and "raw" in data:
        page_payload = parse_product_page(data["raw"])
        data = page_payload
    _record(endpoints, errors, "page", status, data if status == 200 else page_payload, error, attempts)

    bff_status, bff_data, bff_error, bff_attempts = client.request(
        "POST",
        f"https://kaspi.kz/yml/bff-sf/item-page/product/{product_id}",
        headers={**headers, "Content-Type": "application/json; charset=UTF-8"},
        params={"sku": product_id},
        json_body={"cityId": city, "zoneId": [], "limit": 30, "page": 0, "sortOption": "PRICE"},
    )
    _record(endpoints, errors, "item", bff_status, bff_data, bff_error, bff_attempts)

    unit = "PIECES"
    increment: int | float = 1
    content_card = ((endpoints.get("content") or {}).get("data") or {}).get("card") or {}
    if isinstance(content_card, dict) and isinstance(content_card.get("unit"), dict):
        unit = content_card["unit"].get("type") or unit
        increment = content_card["unit"].get("increment") or increment

    merchants = merchants_from_offers(bff_data)
    stock_rows: list[dict[str, Any]] = []
    for merchant in merchants:
        stock_rows.append(
            probe_merchant_stock(
                client,
                headers,
                product_id,
                merchant["merchant_id"],
                unit,
                increment,
            )
        )
        stock_rows[-1]["merchant_name"] = merchant.get("merchant_name")
        stock_rows[-1]["price"] = merchant.get("price")
    available = [row["StockAvailable"] for row in stock_rows if isinstance(row.get("StockAvailable"), int)]
    if not merchants:
        errors.append({"endpoint": "stock", "status": bff_status, "attempts": bff_attempts, "detail": "Нет продавца, остаток склада не запрошен"})

    reviews = product_review_breakdown(client, headers, product_id, city)
    if reviews.get("error"):
        errors.append({"endpoint": "reviews_by_merchant", "status": reviews.get("status") or 0, "attempts": reviews.get("attempts") or 0, "detail": reviews["error"]})
    known = {row["merchant_id"] for row in reviews["by_merchant"]}
    for merchant in merchants:
        if merchant["merchant_id"] not in known:
            reviews["by_merchant"].append(
                {
                    "merchant_id": merchant["merchant_id"],
                    "merchant_name": merchant.get("merchant_name") or "",
                    "total": 0,
                    "stars": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
                }
            )

    result = {
        "product_id": product_id,
        "city_id": city,
        "url": page_url,
        "fetched_at": datetime.now(QYZYLORDA).isoformat(timespec="seconds"),
        "StockAvailable": max(available) if available else None,
        "stock_by_merchant": stock_rows,
        "reviews_by_merchant": reviews["by_merchant"],
        "product_review_summary": reviews.get("summary"),
        "quantity": {},
        **endpoints,
        "errors": errors,
    }
    result["quantity"] = quantity_view(result)
    return result


def write_result(payload: dict[str, Any]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{payload['product_id']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Собрать данные карточки Kaspi.kz в Results/*.json")
    parser.add_argument("urls", nargs="+", help="Ссылка на карточку товара")
    parser.add_argument("--city", help="Код города Kaspi. По умолчанию берётся из ?c=, иначе Алматы")
    parser.add_argument("--timeout", type=float, default=25, help="Таймаут одного ответа, секунды")
    parser.add_argument("--retries", type=int, default=3, help="Сколько раз повторить запрос после 429 или таймаута")
    parser.add_argument("--proxy", help="Свой прокси, например socks5h://хост:порт. Если не задан, скрипт поднимает локальный SOCKS5")
    args = parser.parse_args(argv)

    proxy: LocalConnectProxy | None = None
    proxy_url = args.proxy
    if not proxy_url:
        proxy = LocalConnectProxy(timeout=args.timeout)
        proxy.start()
        proxy_url = proxy.url
        print(f"Локальный прокси: {proxy_url}", file=sys.stderr)
    client = Client(proxy_url, args.timeout, args.retries)
    exit_code = 0
    try:
        for raw in args.urls:
            try:
                product_id, city, page_url = parse_card_url(raw, args.city)
            except CardError as exc:
                print(exc, file=sys.stderr)
                exit_code = 1
                continue
            payload = collect(client, product_id, city, page_url)
            path = write_result(payload)
            print(path)
            for err in payload["errors"]:
                print(f"{err['endpoint']}: {err['detail']}", file=sys.stderr)
            useful = payload.get("StockAvailable") is not None or any(
                isinstance(payload.get(name), dict) and payload[name].get("data") is not None
                for name in ("content", "limits", "reviews", "offers", "page", "item")
            )
            if not useful:
                exit_code = 1
    finally:
        if proxy is not None:
            proxy.stop()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
