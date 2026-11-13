"""Локальный SOCKS5-прокси только до kaspi.kz.

Слушает 127.0.0.1. Туннель открывается лишь на порт 443 и только к
kaspi.kz и его поддоменам. Внешний адрес выхода не меняется: прокси
нужен как промежуточное звено для запросов скрипта, а не как способ
скрыть клиент.
"""

from __future__ import annotations

import select
import socket
import threading


class ProxyError(RuntimeError):
    pass


def _allowed_host(host: str) -> bool:
    name = host.lower().strip().rstrip(".")
    return name == "kaspi.kz" or name.endswith(".kaspi.kz")


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise OSError("соединение закрыто")
        buf += chunk
    return buf


def _socks_reply(sock: socket.socket, code: int) -> None:
    sock.sendall(bytes((5, code, 0, 1, 0, 0, 0, 0, 0, 0)))


class LocalConnectProxy:
    """Локальный SOCKS5. Имя класса сохранено, чтобы не менять вызовы."""

    def __init__(self, timeout: float = 25.0) -> None:
        self.timeout = timeout
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(32)
        self._server.settimeout(0.5)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="kaspi-proxy", daemon=True)

    @property
    def url(self) -> str:
        return f"socks5h://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass
        self._thread.join(timeout=2)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        remote: socket.socket | None = None
        try:
            client.settimeout(self.timeout)
            version, nmethods = _recv_exact(client, 2)
            if version != 5:
                return
            methods = _recv_exact(client, nmethods)
            if 0 not in methods:
                client.sendall(b"\x05\xff")
                return
            client.sendall(b"\x05\x00")
            version, command, _reserved, atyp = _recv_exact(client, 4)
            if version != 5 or command != 1:
                _socks_reply(client, 7)
                return
            if atyp == 1:
                host = socket.inet_ntoa(_recv_exact(client, 4))
            elif atyp == 3:
                length = _recv_exact(client, 1)[0]
                host = _recv_exact(client, length).decode("utf-8", "replace")
            elif atyp == 4:
                _recv_exact(client, 16)
                _socks_reply(client, 8)
                return
            else:
                _socks_reply(client, 8)
                return
            port = int.from_bytes(_recv_exact(client, 2), "big")
            if port != 443 or not _allowed_host(host):
                _socks_reply(client, 2)
                return
            remote = socket.create_connection((host, port), timeout=self.timeout)
            remote.settimeout(self.timeout)
            _socks_reply(client, 0)
            self._tunnel(client, remote)
        except OSError:
            try:
                _socks_reply(client, 1)
            except OSError:
                pass
        finally:
            for sock in (client, remote):
                if sock is None:
                    continue
                try:
                    sock.close()
                except OSError:
                    pass

    def _tunnel(self, client: socket.socket, remote: socket.socket) -> None:
        pair = [client, remote]
        while not self._stop.is_set():
            readable, _, errored = select.select(pair, [], pair, self.timeout)
            if errored or not readable:
                return
            for current in readable:
                other = remote if current is client else client
                try:
                    data = current.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    other.sendall(data)
                except OSError:
                    return
