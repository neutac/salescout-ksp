import tempfile
import threading
import time
import unittest
from pathlib import Path

from kaspi_jobs import Watcher, thousand_card_ms
from kaspi_search import CITIES, normalize_card, search_cards
from kaspi_store import Store, describe_change, observation_signature, review_outcome


class ChangeTests(unittest.TestCase):
    def test_same_snapshot_is_not_stored_twice(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "kaspi.db")
            owner = store.create_user("owner", "password12", "user")
            query = store.create_query("аэрогриль", "750000000", 2, 60, owner["id"])
            card_id = store.upsert_card(
                query["id"],
                {
                    "product_id": "145083048",
                    "title": "Аэрогриль WW-820S 8 л черный",
                    "url": "https://kaspi.kz/shop/p/x-145083048/?c=750000000",
                    "brand": None,
                    "category": "Аэрогрили",
                    "image": None,
                    "price": 23750,
                    "rating": 4.9,
                    "listing_reviews": 321,
                },
            )
            snapshot = {
                "stock_total": 57,
                "review_count": 321,
                "stars": {"1": 3, "2": 0, "3": 0, "4": 9, "5": 309},
                "merchants": [
                    {
                        "merchant_id": "1",
                        "merchant_name": "SLW NEO",
                        "price": 23750,
                        "stock": 49,
                        "delivery": [{"label": "завтра, 14 ноя, доставка"}],
                    }
                ],
            }
            self.assertTrue(store.save_if_changed(card_id, query["id"], snapshot))
            self.assertFalse(store.save_if_changed(card_id, query["id"], snapshot))
            changed = {**snapshot, "stock_total": 40, "merchants": [{**snapshot["merchants"][0], "stock": 32}]}
            self.assertTrue(store.save_if_changed(card_id, query["id"], changed))
            detail = store.detail(query["id"])
            history = detail["cards"][0]["history"]
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["change_note"], "первое чтение")
            self.assertIn("склад", history[1]["change_note"])
            self.assertEqual(detail["overview"]["cards"], 1)
            self.assertEqual(detail["suppliers"][0]["merchant_name"], "SLW NEO")
            self.assertEqual(detail["delivery_groups"][0]["label"], "завтра, 14 ноя, доставка")

    def test_signature_ignores_merchant_order(self) -> None:
        base = {
            "stock_total": 1,
            "review_count": 2,
            "stars": {"5": 2},
            "merchants": [
                {"merchant_id": "b", "price": 2, "stock": 1, "delivery": [{"label": "сегодня, доставка"}]},
                {"merchant_id": "a", "price": 1, "stock": 0, "delivery": [{"label": "завтра, самовывоз"}]},
            ],
        }
        flipped = {**base, "merchants": list(reversed(base["merchants"]))}
        self.assertEqual(observation_signature(base), observation_signature(flipped))
        self.assertIn("отзывы", describe_change({"stock_total": 1, "review_count": 1, "stars": {"5": 2}}, base))

    def test_thousand_card_time(self) -> None:
        self.assertEqual(thousand_card_ms(2400, 24), 100_000)
        self.assertIsNone(thousand_card_ms(1000, 0))

    def test_star_counts_split_purchases_and_returns(self) -> None:
        outcome = review_outcome({"1": 3, "2": 1, "3": 4, "4": 9, "5": 309})
        self.assertEqual(outcome["purchases"], 1 + 4 + 9 + 309)
        self.assertEqual(outcome["returns"], 3)

    def test_normalize_search_card(self) -> None:
        card = normalize_card(
            {
                "id": "145083048",
                "title": "Аэрогриль WW-820S 8 л черный",
                "shopLink": "/p/aerogril-ww-820s-8-l-chernyi-145083048/",
                "unitSalePrice": 23750,
                "reviewsQuantity": 321,
                "rating": 4.9,
                "brand": "White Wave",
                "categoryRu": ["Бытовая техника", "Аэрогрили"],
                "previewImages": [{"medium": "https://resources.cdn-kaspi.kz/img/m/p/x.png"}],
            },
            "750000000",
        )
        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card["product_id"], "145083048")
        self.assertTrue(card["url"].startswith("https://kaspi.kz/shop/p/"))
        self.assertIn("c=750000000", card["url"])
        self.assertEqual(card["category"], "Бытовая техника / Аэрогрили")

    def test_search_covers_every_city(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.cities: list[str] = []

            def request(self, method: str, url: str, *, headers: dict, params: dict, json_body=None):
                del method, url, headers, json_body
                self.cities.append(params["c"])
                return 200, {"data": {"cards": [{"id": "100", "title": "Телефон"}], "total": 3}}, None, 1

        client = FakeClient()
        found = search_cards(client, "iphone", "750000000")
        self.assertEqual(client.cities, [city_id for city_id, _name in CITIES])
        self.assertIsNone(found["error"])
        self.assertEqual(len(found["cards"]), 1)
        self.assertEqual(found["cards"][0]["cities"], ", ".join(name for _city_id, name in CITIES))
        self.assertEqual(found["cards"][0]["stock_city"], CITIES[0][0])

    def test_manual_stop(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "kaspi.db")
            owner = store.create_user("owner", "password12", "user")
            query = store.create_query("аэрогриль", "750000000", 3, 30, owner["id"])
            started = threading.Event()

            class SlowClient:
                pass

            watcher = Watcher(store, SlowClient())  # type: ignore[arg-type]

            def blocking(query_id: int, stop: threading.Event) -> int:
                started.set()
                stop.wait(5)
                return 0

            watcher._check = blocking  # type: ignore[method-assign]
            watcher.start(query["id"])
            self.assertTrue(started.wait(2))
            watcher.stop(query["id"])
            time.sleep(0.3)
            self.assertEqual(store.get_query(query["id"])["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
