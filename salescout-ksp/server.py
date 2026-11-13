"""Страница наблюдения: вход, пользователи, поиск карточек, остатки и отзывы."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from kaspi_auth import issue_session, read_session, session_key
from kaspi_card import DEFAULT_CITY, Client
from kaspi_jobs import Watcher
from kaspi_proxy import LocalConnectProxy
from kaspi_store import QYZYLORDA, Store

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "web" / "dist"
SESSION_COOKIE = "kaspi_session"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
MAX_ACTIVE_QUERIES = 1
MAX_QUERIES_PER_HOUR = 4
MAX_GLOBAL_ACTIVE = 2
MAX_GLOBAL_PER_HOUR = 6
LOGIN_WINDOW_SECONDS = 600
LOGIN_MAX_PER_NAME = 8
LOGIN_MAX_PER_IP = 12
LOGIN_MAX_GLOBAL = 30

def _db_path() -> Path:
    candidates: list[Path] = []
    configured = os.environ.get("KASPI_DB")
    if configured:
        candidates.append(Path(configured))
    candidates.extend([ROOT / "data" / "kaspi.db", Path("/tmp/kaspi.db")])
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            probe = path.parent / ".kaspi-write"
            probe.write_text("1", encoding="utf-8")
            probe.unlink()
            return path
        except OSError:
            continue
    return Path("/tmp/kaspi.db")


def _open_client() -> Client:
    try:
        proxy = LocalConnectProxy(timeout=25)
        proxy.start()
        return Client(proxy.url, timeout=25, retries=2)
    except OSError:
        return Client(None, timeout=25, retries=2)


store = Store(_db_path())
try:
    client = _open_client()
    watcher = Watcher(store, client)
except Exception:
    client = None
    watcher = None
_login_attempts: dict[str, list[float]] = {}

app = FastAPI(title="Наблюдение Kaspi")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8643", "http://localhost:8643"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LoginIn(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class QueryIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    checks: int = Field(ge=1, le=6)
    interval_minutes: int = Field(ge=10, le=1440)
    city_id: str = Field(default="all", min_length=3, max_length=20)


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(default="user")


class UserPatch(BaseModel):
    disabled: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)


def _session_key() -> bytes | None:
    return session_key(os.environ.get("SESSION_SECRET"))


def _actor(request: Request) -> dict:
    key = _session_key()
    claims = read_session(request.cookies.get(SESSION_COOKIE), key) if key is not None else None
    if claims is None:
        raise HTTPException(401, "Нужен вход")
    try:
        user = store.get_user(int(claims["id"]))
    except KeyError:
        raise HTTPException(401, "Нужен вход") from None
    if user["disabled"] or user["username"] != claims.get("username") or user["role"] not in {"admin", "user"}:
        raise HTTPException(401, "Нужен вход")
    return user


def _mutation(request: Request) -> None:
    if request.headers.get("x-requested-with") != "kaspi":
        raise HTTPException(403, "Запрос отклонён")


def _owns(user: dict, query: dict) -> bool:
    return user["role"] == "admin" or query.get("user_id") == user["id"]


def _query_for(user: dict, query_id: int) -> dict:
    try:
        query = store.get_query(query_id)
    except KeyError:
        raise HTTPException(404, "Запрос не найден") from None
    if not _owns(user, query):
        raise HTTPException(404, "Запрос не найден")
    return query


def _set_session(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
        max_age=14 * 24 * 3600,
        path="/",
    )


def _client_ip(request: Request) -> str:
    if request.client is not None and request.client.host:
        return request.client.host[:64]
    return "unknown"


def _recent(key: str, now: float) -> list[float]:
    recent = [moment for moment in _login_attempts.get(key, []) if now - moment < LOGIN_WINDOW_SECONDS]
    if recent:
        _login_attempts[key] = recent
    else:
        _login_attempts.pop(key, None)
    return recent


def _login_blocked(username: str, ip: str) -> bool:
    now = time.time()
    return (
        len(_recent(f"user:{username.lower()}", now)) >= LOGIN_MAX_PER_NAME
        or len(_recent(f"ip:{ip}", now)) >= LOGIN_MAX_PER_IP
        or len(_recent("global", now)) >= LOGIN_MAX_GLOBAL
    )


def _note_login_failure(username: str, ip: str) -> None:
    now = time.time()
    for key in (f"user:{username.lower()}", f"ip:{ip}", "global"):
        _login_attempts.setdefault(key, []).append(now)


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    _mutation(request)
    username = body.username.strip()
    ip = _client_ip(request)
    if _login_blocked(username, ip):
        raise HTTPException(429, "Слишком много попыток входа. Подождите и попробуйте снова.")
    key = _session_key()
    if key is None:
        raise HTTPException(503, "Вход временно недоступен")
    user = store.authenticate(username, body.password)
    if user is None:
        _note_login_failure(username, ip)
        raise HTTPException(401, "Неверное имя или пароль")
    _login_attempts.pop(f"user:{username.lower()}", None)
    _set_session(response, request, issue_session(user, key))
    return {"user": user}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
    _mutation(request)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request) -> dict:
    return {"user": _actor(request)}


@app.get("/api/users")
def list_users(request: Request) -> dict:
    user = _actor(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Только администратор управляет пользователями")
    return {"users": store.list_users()}


@app.post("/api/users")
def create_user(body: UserIn, request: Request) -> dict:
    _mutation(request)
    actor = _actor(request)
    if actor["role"] != "admin":
        raise HTTPException(403, "Только администратор управляет пользователями")
    username = body.username.strip()
    if USERNAME_RE.fullmatch(username) is None:
        raise HTTPException(400, "Имя: латиница, цифры и подчёркивание, от 2 до 32 знаков")
    if body.role not in {"admin", "user"}:
        raise HTTPException(400, "Роль: admin или user")
    try:
        created = store.create_user(username, body.password, body.role)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"user": created}


@app.patch("/api/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, request: Request) -> dict:
    _mutation(request)
    actor = _actor(request)
    if actor["role"] != "admin":
        raise HTTPException(403, "Только администратор управляет пользователями")
    try:
        target = store.get_user(user_id)
    except KeyError:
        raise HTTPException(404, "Пользователь не найден") from None
    if target["username"] == "root" and body.disabled:
        raise HTTPException(400, "Учётную запись root отключить нельзя")
    if target["id"] == actor["id"] and body.disabled:
        raise HTTPException(400, "Нельзя отключить свою учётную запись")
    updated = store.update_user(user_id, disabled=body.disabled, password=body.password)
    return {"user": updated}


@app.get("/api/queries")
def list_queries(request: Request) -> dict:
    user = _actor(request)
    scope = None if user["role"] == "admin" else user["id"]
    return {"queries": store.list_queries(scope)}


@app.post("/api/queries")
def create_query(body: QueryIn, request: Request) -> dict:
    _mutation(request)
    user = _actor(request)
    if user["disabled"]:
        raise HTTPException(403, "Учётная запись отключена")
    if store.active_query_total() >= MAX_GLOBAL_ACTIVE or store.active_query_count(user["id"]) >= MAX_ACTIVE_QUERIES:
        raise HTTPException(429, "Сервер уже ведёт максимум проверок. Остановите одну, чтобы начать новую.")
    since = (datetime.now(QYZYLORDA) - timedelta(hours=1)).isoformat(timespec="seconds")
    if store.recent_query_total(since) >= MAX_GLOBAL_PER_HOUR or store.recent_query_count(user["id"], since) >= MAX_QUERIES_PER_HOUR:
        raise HTTPException(429, "За час на сервере можно создать не больше шести проверок.")
    name = " ".join(body.name.split())
    if len(name) < 2:
        raise HTTPException(400, "Введите наименование товара")
    if watcher is None:
        raise HTTPException(503, "Сбор карточек на этом сервере сейчас недоступен")
    query = store.create_query(name, body.city_id.strip(), body.checks, body.interval_minutes * 60, user["id"])
    try:
        watcher.list_now(int(query["id"]))
    except RuntimeError as exc:
        store.update_query(int(query["id"]), status="error", last_error=str(exc), progress="проверка прервана")
        raise HTTPException(502, str(exc)) from exc
    watcher.start(int(query["id"]))
    return {"query": store.get_query(int(query["id"]))}


@app.get("/api/queries/{query_id}")
def query_detail(query_id: int, request: Request) -> dict:
    user = _actor(request)
    _query_for(user, query_id)
    return store.detail(query_id)


@app.post("/api/queries/{query_id}/stop")
def stop_query(query_id: int, request: Request) -> dict:
    _mutation(request)
    user = _actor(request)
    _query_for(user, query_id)
    if watcher is None:
        raise HTTPException(503, "Сбор карточек на этом сервере сейчас недоступен")
    try:
        return {"query": watcher.stop(query_id)}
    except KeyError:
        raise HTTPException(404, "Запрос не найден") from None


@app.on_event("startup")
def resume_queries() -> None:
    try:
        password = os.environ.get("ADMIN_PASSWORD")
        username = os.environ.get("ADMIN_USERNAME", "root").strip() or "root"
        if password:
            store.ensure_admin(username, password)
        if watcher is not None:
            watcher.resume_open(MAX_GLOBAL_ACTIVE)
    except Exception:
        return


def _mount_page() -> None:
    if DIST.exists():
        app.mount("/", StaticFiles(directory=DIST, html=True), name="page")


_mount_page()
