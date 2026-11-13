import socket
import unittest

from kaspi_proxy import LocalConnectProxy


def _ask(port: int, host: str, dest_port: int = 443) -> int:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall(b"\x05\x01\x00")
        if sock.recv(2) != b"\x05\x00":
            raise AssertionError("сервер не принял метод без пароля")
        name = host.encode("idna")
        sock.sendall(bytes((5, 1, 0, 3, len(name))) + name + dest_port.to_bytes(2, "big"))
        reply = sock.recv(10)
        return reply[1]
    finally:
        sock.close()


class SocksProxyTests(unittest.TestCase):
    def test_local_proxy_is_socks5_and_rejects_other_hosts(self) -> None:
        proxy = LocalConnectProxy(timeout=5)
        proxy.start()
        try:
            self.assertTrue(proxy.url.startswith("socks5h://127.0.0.1:"))
            self.assertEqual(_ask(proxy.port, "example.com"), 2)
        finally:
            proxy.stop()


if __name__ == "__main__":
    unittest.main()
