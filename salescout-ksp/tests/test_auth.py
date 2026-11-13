import tempfile
import unittest
from pathlib import Path

from kaspi_auth import issue_session, read_session, session_key
from kaspi_store import Store


class StoreAuthTests(unittest.TestCase):
    def test_users_see_only_their_queries(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "kaspi.db")
            admin = store.ensure_admin("root", "correct-horse")
            other = store.create_user("shop", "password12", "user")
            own = store.create_query("свой товар", "750000000", 1, 60, other["id"])
            store.create_query("чужой товар", "750000000", 1, 60, admin["id"])
            visible = store.list_queries(other["id"])
            self.assertEqual([item["id"] for item in visible], [own["id"]])
            self.assertEqual(len(store.list_queries(None)), 2)
            self.assertIsNone(store.authenticate("shop", "wrong-password"))
            self.assertIsNone(store.authenticate("missing", "password12"))
            self.assertEqual(store.authenticate("root", "correct-horse")["role"], "admin")
            token = store.open_session(other["id"])
            self.assertEqual(store.user_by_token(token)["username"], "shop")
            store.update_user(other["id"], disabled=True)
            self.assertIsNone(store.user_by_token(token))

    def test_signed_session_survives_without_session_table(self) -> None:
        key = b"test-key"
        token = issue_session({"id": 7, "username": "root", "role": "admin"}, key, now=1_700_000_000)
        claims = read_session(token, key, now=1_700_000_100)
        self.assertEqual(claims["username"], "root")
        self.assertIsNone(read_session(token, b"other-key", now=1_700_000_100))
        self.assertIsNone(read_session(token, key, now=1_700_000_000 + 15 * 24 * 3600))
        self.assertIsNone(session_key(None))
        self.assertIsNone(session_key("short-secret"))
        self.assertIsNone(session_key("local-session"))
        self.assertIsNotNone(session_key("x" * 16))
        self.assertEqual(session_key("x" * 32), session_key("x" * 32))
        self.assertNotEqual(session_key("x" * 32), session_key("y" * 32))


if __name__ == "__main__":
    unittest.main()
