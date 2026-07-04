from __future__ import annotations

import json
import logging
import os
import time
import asyncio
import threading
import secrets
import string
import base64
import hmac
import struct
import sys
import subprocess
import importlib
import tempfile
import urllib.parse
import html as _html
import re
from collections import UserList, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha1
from typing import Any, ClassVar, Dict, List, Optional, Set, Tuple

# ── Проверка версии Python ──────────────────────────────────────────────────
_PY_VER_WARNING = sys.version_info < (3, 11)
_PY_VER_STR = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
if _PY_VER_WARNING:
    print(
        f"\n[ASR+ v1.2.0] ⚠️ Обнаружена Python {_PY_VER_STR}\n"
        f"Для стабильной работы плагина ASR+ требуется Python 3.11.\n"
        f"Пожалуйста, обновитесь до Python 3.11, иначе возможны ошибки.\n"
    )

for pkg, imp in [("aiohttp", "aiohttp"), ("pytz", "pytz"), ("pysteamauth", "pysteamauth"),
                 ("rsa", "rsa"), ("requests", "requests"), ("yarl", "yarl"),
                 ("playwright", "playwright"), ("cryptography", "cryptography")]:
    try:
        importlib.import_module(imp)
    except ImportError:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        except Exception:
            pass

try:
    from playwright.sync_api import sync_playwright as _sync_pw
    _pw_check_needed = False
    try:
        with _sync_pw() as _p:
            _cb = _p.chromium.executable_path
            if not os.path.exists(_cb):
                _pw_check_needed = True
    except Exception:
        _pw_check_needed = True
    if _pw_check_needed:
        subprocess.check_call(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300
        )
except Exception:
    pass

import aiohttp
import rsa
from pytz import timezone
from pydantic import BaseModel, Field
from pysteamauth.auth import Steam as _BaseSteam
from yarl import URL as YarlURL
from cardinal import Cardinal
from FunPayAPI.common.enums import OrderStatuses, MessageTypes
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent, OrderStatusChangedEvent
from tg_bot import CBT as _CBT
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B
from cryptography.fernet import Fernet, InvalidToken

NAME = "ASR+"
VERSION = "1.2.0"
CREDITS = "@DzhantDev"
DESCRIPTION = "Плагин для автоматической почасовой аренды Steam аккаунтов (1 час = 1 единица товара)"
UUID = "d12da53a-391f-416c-b49c-d57f697f9208"
SETTINGS_PAGE = True
PAGE_SIZE = 8
MAX_ORDERS_STORED = 500
MAX_PROCESSED_IDS = 1000
ORDERS_MAX_AGE_DAYS = 14

logger = logging.getLogger("FPC.ASRplus")

def _hours_word(n: float) -> str:
    """Возвращает правильную форму слова 'час' для числа n."""
    n_int = int(n)
    if n != n_int:
        return "часа"
    mod10 = n_int % 10
    mod100 = n_int % 100
    if mod100 in range(11, 20):
        return "часов"
    if mod10 == 1:
        return "час"
    if mod10 in (2, 3, 4):
        return "часа"
    return "часов"
try:
    MOSCOW_TZ = timezone('Europe/Moscow')
except Exception:
    MOSCOW_TZ = timezone('UTC')

ICON_STATUS = {"FREE": "🟢", "ACTIVE": "👤", "BUSY": "⏳", "ERROR": "❌"}

CODE_COOLDOWN = 5.0
SELLER_CALL_COOLDOWN = 60.0
PASSWORD_CHANGE_TIMEOUT = 180

class SteamEmailVerificationRequired(Exception):
    pass

FUNPAY_LOT_URL = "https://funpay.com/lots/offer?id={lot_id}"
FUNPAY_ORDER_URL = "https://funpay.com/orders/{}/"
FUNPAY_CHAT_URL = "https://funpay.com/chat/?node={}"

_CMD_CODE = frozenset(("!steamguard", "!code", "/code", "!код", "/код", "код", "code"))
_CMD_TIME = frozenset(("!time", "/time", "!время", "/время", "время", "time"))
_CMD_EXTEND = frozenset(("!extend", "/extend", "!продлить", "/продлить", "продлить", "extend"))
_CMD_STOCK = frozenset(("!stock", "/stock", "!наличие", "/наличие", "наличие", "stock"))
_CMD_ACCOUNT = frozenset(("!аккаунт", "/аккаунт", "!account", "/account", "аккаунт"))
_CMD_SELLER = frozenset(("!продавец", "/продавец", "продавец", "!seller", "/seller", "seller"))

# Единый список команд, доступных покупателю (для показа при выдаче аккаунта и т.п.)
BUYER_COMMANDS_TEXT = (
    "▸  !код       — получить Steam Guard код\n"
    "▸  !время     — сколько времени осталось\n"
    "▸  !продлить  — продлить аренду\n"
    "▸  !аккаунт   — повторно получить логин/пароль\n"
    "▸  !наличие   — узнать, что есть в наличии\n"
    "▸  !продавец  — позвать продавца"
)

def _safe_err(e: Exception) -> str:
    text = str(e)
    text = re.sub(r'<[^>]+>', '', text)
    text = _html.escape(text)
    return text[:300]

# Коды категорий ошибок -> (понятное описание на русском, можно ли считать ошибку
# временной/сетевой, для которой имеет смысл автоматически повторить попытку)
def _classify_error(e: Exception) -> Tuple[str, str, bool]:
    """Определяет, что именно пошло не так, чтобы продавец видел точную причину,
    а не голый traceback. Возвращает (код, читаемое_описание, можно_ли_повторить)."""
    msg = str(e) or ""
    low = msg.lower()
    name = type(e).__name__

    if isinstance(e, SteamEmailVerificationRequired):
        return "EMAIL_CONFIRM", "Steam требует подтверждение через почту — автосмена пароля невозможна, нужна ручная смена", False
    if isinstance(e, ValueError) and ("отсутствует" in low or "mafile" in low):
        return "MAFILE_INVALID", f"В maFile аккаунта не хватает обязательных данных: {msg}", False
    if "неверный пароль" in low:
        return "WRONG_PASSWORD", "Указан неверный текущий пароль от аккаунта Steam — сохранённый пароль устарел", False
    if "mobile confirmation" in low:
        return "MOBILE_CONFIRM_FAILED", "Steam не принял подтверждение через Steam Guard Mobile (неверный identity_secret либо конфликт устройств)", False
    if "steam login failed" in low:
        return "LOGIN_FAILED", "Не удалось авторизоваться в Steam (неверные учётные данные либо временная блокировка входа)", False
    if "rsa key" in low:
        return "RSA_MISSING", "Steam не выдал RSA-ключ для входа — аккаунт может быть временно заблокирован или требует доп. проверки", True
    if "wizard params" in low:
        return "NO_WIZARD", "Steam не выдал параметры восстановления пароля (аккаунт заблокирован либо требуется email-подтверждение)", False
    if "sessionid" in low:
        return "NO_SESSION", "Не удалось получить сессию Steam (сервис недоступен либо забанен IP-адрес бота)", True
    if "poll confirmation timed out" in low or "poll recovery" in low:
        return "POLL_TIMEOUT", "Не дождались подтверждения в мобильном приложении Steam Guard (истекло время ожидания)", True
    if "verifycode" in low or ("verify" in low and "code" in low):
        return "VERIFY_CODE_FAILED", "Steam отклонил код подтверждения при смене пароля", False
    if "changepassword" in low:
        return "CHANGE_REJECTED", "Steam отклонил запрос на смену пароля", False
    if "jsondecodeerror" in low or ("json" in low and "decode" in low):
        return "BAD_RESPONSE", "Steam вернул некорректный/повреждённый ответ (похоже на капчу или антибот-защиту)", True
    if "html response" in low:
        return "HTML_RESPONSE", "Steam вернул HTML-страницу вместо ожидаемых данных (капча, антибот-защита или сбой сервиса Steam)", True
    if "cancelled" in low or name == "CancelledError":
        return "CANCELLED", "Операция была прервана по таймауту воркера", True
    if "timed out" in low or "timeout" in low or name in ("TimeoutError", "asyncio.TimeoutError"):
        return "TIMEOUT", "Превышено время ожидания ответа от Steam (таймаут)", True
    if any(k in low for k in ("connection", "connect", "network", "dns", "resolve")) or \
       name in ("ClientConnectorError", "ConnectionError", "ClientError", "ClientOSError"):
        return "NETWORK", "Проблема сети при обращении к серверам Steam (нет связи/сброс соединения)", True
    return "UNKNOWN", f"Неизвестная техническая ошибка ({name}): {_safe_err(e)}", False

def _now() -> datetime:
    return datetime.now(MOSCOW_TZ)

def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")

_DT_FMT = "%Y-%m-%d %H:%M:%S"

def _parse(s: str) -> datetime:
    try:
        return MOSCOW_TZ.localize(datetime.strptime(s, _DT_FMT))
    except Exception:
        return _now()

def _ntag(tag: str) -> str:
    return tag.strip().lower()

# ─────────────────────────────────────────────
#  #ТЕГ-ID ДЛЯ ТОЧНОГО ОПОЗНАВАНИЯ ЛОТА В ЗАКАЗЕ
# ─────────────────────────────────────────────
# Каждому настроенному лоту генерируется собственный уникальный ID
# (например #id7k2p9a). Он пишется в конец подробного описания лота на
# FunPay (кнопка "Авто-тег") и попадает в текст заказа при покупке.
# В отличие от обычного `tag` (который выбирает пул аккаунтов и может
# повторяться у нескольких лотов), ID уникален для каждого лота и
# никогда не совпадёт с номером заказа — поэтому по нему лот определяется
# однозначно, без риска спутать его с "#41234567" (номер заказа).
_MATCH_TAG_PREFIX = "id"
_MATCH_TAG_RANDOM_LEN = 6

def _gen_match_tag(existing: set) -> str:
    """
    Генерирует уникальный служебный ID лота —
    например "id7k2p9a". Он никак не связан с обычным тегом лота (тегом
    пула аккаунтов) и не зависит от него — просто короткий случайный
    идентификатор, уникальный для каждого лота, который дописывается в
    подробное описание лота и однозначно опознаёт заказ.
    """
    import random as _random
    import string as _string
    chars = _string.ascii_lowercase + _string.digits
    while True:
        tag = _MATCH_TAG_PREFIX + "".join(_random.choices(chars, k=_MATCH_TAG_RANDOM_LEN))
        if tag not in existing:
            return tag

def _extract_lot_id(text: str) -> Optional[str]:
    if not text:
        return None
    s = text.strip()
    m = re.search(r"[?&]id=(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"/offer/?(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{5,})\b", s)
    if m:
        return m.group(1)
    return None

def _remaining_str(end: str) -> str:
    rem = (_parse(end) - _now()).total_seconds()
    if rem <= 0:
        return "Истекло"
    total_min = int(rem // 60)
    d, rem_min = divmod(total_min, 1440)
    h, m = divmod(rem_min, 60)
    parts = []
    if d:
        parts.append(f"{d}д")
    if h:
        parts.append(f"{h}ч")
    if m or not parts:
        parts.append(f"{m}м")
    return " ".join(parts)

def _gen_password(length: int = 20) -> str:
    alpha = string.ascii_letters + string.digits
    while True:
        pwd = ''.join(secrets.choice(alpha) for _ in range(length))
        if (any(c.isupper() for c in pwd) and any(c.islower() for c in pwd)
                and any(c.isdigit() for c in pwd)):
            return pwd

def _is_on(v: bool) -> str:
    return "🟢" if v else "🔴"

def _get_path(filename: str) -> str:
    return os.path.join(os.path.dirname(__file__), "..", "storage", "plugins",
                        "asrplus", f"{filename}.json" if "." not in filename else filename)

os.makedirs(os.path.dirname(_get_path("x")), exist_ok=True)

# ── Шифрование чувствительных данных (пароли Steam, mafile-секреты) ─────────
# Файлы из _SENSITIVE_FILES хранятся на диске в зашифрованном виде (Fernet/AES).
# Ключ лежит отдельно, с правами 0600 — доступен только владельцу процесса.
_SENSITIVE_FILES = {"accounts", "pwd_backups"}

def _get_or_create_key() -> bytes:
    key_path = os.path.join(os.path.dirname(_get_path("x")), "secret.key")
    if os.path.exists(key_path):
        try:
            with open(key_path, "rb") as f:
                k = f.read().strip()
            if k:
                return k
        except OSError as e:
            logger.error(f"[ASRplus] Не удалось прочитать ключ шифрования: {e}")
    key = Fernet.generate_key()
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    except FileExistsError:
        # Параллельная инициализация в другом потоке/процессе — читаем то, что уже записали
        with open(key_path, "rb") as f:
            key = f.read().strip()
    except OSError as e:
        logger.error(f"[ASRplus] Не удалось создать ключ шифрования: {e}")
    try:
        os.chmod(key_path, 0o600)
    except Exception:
        pass
    return key

_FERNET = Fernet(_get_or_create_key())

def _secure_chmod(path: str):
    """Ограничивает права на файл данных только владельцу процесса (no-op на Windows)."""
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass

def _load_json(filename: str) -> Any:
    p = _get_path(filename)
    if not os.path.exists(p):
        return {}
    sensitive = filename in _SENSITIVE_FILES
    try:
        if sensitive:
            with open(p, "rb") as f:
                raw = f.read()
            if not raw.strip():
                return {}
            try:
                content = _FERNET.decrypt(raw).decode("utf-8")
            except InvalidToken:
                # Файл мог остаться от версии без шифрования (миграция со старой версии плагина)
                try:
                    content = raw.decode("utf-8")
                    logger.warning(f"[ASRplus] {filename}.json прочитан как незашифрованный "
                                   f"(будет зашифрован при следующем сохранении)")
                except UnicodeDecodeError:
                    logger.error(f"[ASRplus] Не удалось расшифровать {filename}: повреждён ключ или файл")
                    return {}
        else:
            with open(p, encoding="utf-8") as f:
                content = f.read().strip()
        if not content:
            return {}
        return json.loads(content)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[ASRplus] Не удалось прочитать {filename}: {e}")
        return {}

_file_lock = threading.Lock()

def _save_json(filename: str, data: Any):
    p = _get_path(filename)
    sensitive = filename in _SENSITIVE_FILES
    with _file_lock:
        dir_name = os.path.dirname(p)
        try:
            text = json.dumps(data, indent=4, ensure_ascii=False, default=str)
            payload = _FERNET.encrypt(text.encode("utf-8")) if sensitive else text.encode("utf-8")
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            if os.path.exists(p):
                os.replace(tmp_path, p)
            else:
                os.rename(tmp_path, p)
            _secure_chmod(p)
        except Exception as _e:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass
            try:
                with open(p, "wb") as f:
                    f.write(payload)
                _secure_chmod(p)
            except Exception as _e2:
                logger.error(f"[ASRplus] Не удалось сохранить {filename}: {_e2}")

class LotsCache(UserList):
    def __init__(self):
        super().__init__()
        self.updated_at: Optional[float] = None

_lots_cache = LotsCache()
_LOTS_CACHE_TTL = 180.0

def _get_cached_lots(c):
    global _lots_cache
    if not _lots_cache or _lots_cache.updated_at is None or (time.time() - _lots_cache.updated_at) >= _LOTS_CACHE_TTL:
        _lots_cache.data.clear()
        _lots_cache.extend(c.account.get_user(c.account.id).get_lots())
        _lots_cache.updated_at = time.time()
    return _lots_cache

def _invalidate_lots_cache():
    global _lots_cache
    _lots_cache.data.clear()
    _lots_cache.updated_at = None

def _toggle_fp_lots_for_tag(c, tag: str, enable: bool) -> List[str]:
    tag = _ntag(tag)
    with _toggling_lock:
        if tag in _toggling_tags:
            return []
        _toggling_tags.add(tag)
    try:
        lot_ids = [lid for lid in SETTINGS.lots
                   if _ntag((SETTINGS.get_lot(lid) or LotConfig(tag="default")).tag) == tag]
        toggled = []
        for lid in lot_ids:
            try:
                lf = c.account.get_lot_fields(int(lid))
                if lf.active != enable:
                    lf.active = enable
                    c.account.save_lot(lf)
                    toggled.append(lid)
                    logger.debug(f"[ASRplus] Лот #{lid} {'включён' if enable else 'выключен'}")
            except Exception as e:
                logger.warning(f"[ASRplus] Ошибка переключения лота #{lid}: {e}")
        _invalidate_lots_cache()
        return toggled
    finally:
        with _toggling_lock:
            _toggling_tags.discard(tag)

class RentStatus:
    FREE = "FREE"
    BUSY = "BUSY"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"
    FINISHED = "FINISHED"
    REFUND = "REFUND"

class SteamGuard:
    _time_offset: int = 0
    _last_sync: float = 0
    SYNC_INTERVAL: int = 300
    SYMBOLS = "23456789BCDFGHJKMNPQRTVWXY"

    @classmethod
    def sync_time_sync(cls) -> int:
        try:
            import requests as req
            resp = req.post("https://api.steampowered.com/ITwoFactorService/QueryTime/v0001", timeout=10)
            if resp.status_code == 200:
                st = int(resp.json()["response"]["server_time"])
                cls._time_offset = st - int(time.time())
                cls._last_sync = time.time()
        except Exception:
            pass
        return cls._time_offset

    @classmethod
    async def sync_time_async(cls) -> int:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post("https://api.steampowered.com/ITwoFactorService/QueryTime/v0001",
                                  timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        d = await resp.json()
                        cls._time_offset = int(d["response"]["server_time"]) - int(time.time())
                        cls._last_sync = time.time()
        except Exception:
            pass
        return cls._time_offset

    @classmethod
    def _steam_time(cls) -> int:
        return int(time.time()) + cls._time_offset

    @classmethod
    def _seconds_until_next_window(cls) -> int:
        return 30 - (cls._steam_time() % 30)

    @classmethod
    def _generate(cls, shared_secret: str) -> str:
        ts = cls._steam_time()
        tw = ts // 30
        s = shared_secret
        if len(s) % 4:
            s += '=' * (4 - len(s) % 4)
        sb = base64.b64decode(s)
        hr = hmac.new(sb, struct.pack(">Q", tw), sha1).digest()
        o = hr[19] & 0x0F
        v = struct.unpack(">I", hr[o:o + 4])[0] & 0x7FFFFFFF
        c = ""
        for _ in range(5):
            c += cls.SYMBOLS[v % len(cls.SYMBOLS)]
            v //= len(cls.SYMBOLS)
        return c

    @classmethod
    def code_sync(cls, shared_secret: str) -> str:
        if not shared_secret:
            return "NO_SECRET"
        if time.time() - cls._last_sync > cls.SYNC_INTERVAL:
            cls.sync_time_sync()
        try:
            return cls._generate(shared_secret)
        except Exception:
            return "ERROR"

    @classmethod
    async def code_async(cls, shared_secret: str) -> str:
        if not shared_secret:
            return "NO_SECRET"
        if time.time() - cls._last_sync > cls.SYNC_INTERVAL:
            await cls.sync_time_async()
        try:
            return cls._generate(shared_secret)
        except Exception:
            return "ERROR"

def _generate_confirmation_key(identity_secret: str, timestamp: int, tag: str) -> str:
    s = identity_secret
    if len(s) % 4:
        s += '=' * (4 - len(s) % 4)
    sb = base64.b64decode(s)
    data = struct.pack(">Q", timestamp) + tag.encode("utf-8")
    return base64.b64encode(hmac.new(sb, data, sha1).digest()).decode("utf-8")

class CustomSteam(_BaseSteam):
    def __init__(self, login, password, shared_secret, identity_secret, device_id, steamid):
        super().__init__(login=login, password=password, steamid=steamid,
                         shared_secret=shared_secret, identity_secret=identity_secret,
                         device_id=device_id)
        self._login = login
        self._pwd = password

    @property
    def login(self):
        return self._login

    @property
    def password(self):
        return self._pwd

    async def raw_request(self, method: str, url: str, **kw):
        from urllib3.util import parse_url
        parsed = parse_url(url)
        host = parsed.host or "steamcommunity.com"
        try:
            cookies = await self.cookies(host)
        except Exception:
            cookies = {}
        return await self._requests.request(method=method, url=url, cookies=cookies, **kw)

class PasswordChangeParams:
    def __init__(self, s, account, reset, issueid, lost=0, **kwargs):
        self.s = int(s)
        self.account = int(account)
        self.reset = int(reset)
        self.issueid = int(issueid)
        self.lost = int(lost)

def _validate_mafile(mf: dict) -> List[str]:
    missing = []
    for f in ("shared_secret", "identity_secret", "account_name"):
        if not mf.get(f):
            missing.append(f)
    return missing

def _warn_mafile(mf: dict) -> List[str]:
    warn = []
    if not mf.get("device_id"):
        warn.append("device_id")
    if not (mf.get("Session") or {}).get("SteamID"):
        warn.append("Session.SteamID")
    return warn

_acc_pwd_locks: Dict[int, threading.Lock] = {}
_acc_pwd_locks_mutex = threading.Lock()
_pwd_change_lock = threading.Lock()

def _get_acc_lock(acc_id: int) -> threading.Lock:
    with _acc_pwd_locks_mutex:
        if acc_id not in _acc_pwd_locks:
            _acc_pwd_locks[acc_id] = threading.Lock()
        return _acc_pwd_locks[acc_id]

class SteamPasswordChanger:
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    HELP = "https://help.steampowered.com"

    def __init__(self, mafile: dict, current_password: str):
        self.mafile = mafile
        self.current_password = current_password
        self.login = mafile.get("account_name", "")
        self.shared_secret = mafile.get("shared_secret", "")
        self.identity_secret = mafile.get("identity_secret", "")
        self.device_id = mafile.get("device_id", "")
        self.steamid = int((mafile.get("Session") or {}).get("SteamID", 0))
        self._steam: Optional[CustomSteam] = None
        missing = _validate_mafile(mafile)
        if missing:
            raise ValueError(f"Отсутствует в maFile: {', '.join(missing)}")
        full_check = _warn_mafile(mafile)
        if full_check:
            raise ValueError(f"Отсутствует в maFile (нужно для смены пароля): {', '.join(full_check)}")

    async def change_password(self) -> str:
        new_password = _gen_password(20)
        self._steam = CustomSteam(
            login=self.login, password=self.current_password,
            shared_secret=self.shared_secret, identity_secret=self.identity_secret,
            device_id=self.device_id, steamid=self.steamid)
        await self._login_steam()
        params = await self._get_wizard_params()
        logger.info(f"[ASRplus] Wizard params: s={params.s} issueid={params.issueid}")
        await self._playwright_open_wizard(params)
        confirmed = await self._confirm_recovery(params)
        if not confirmed:
            raise Exception(f"Mobile confirmation не принята для {self.login}")
        logger.info(f"[ASRplus] Мобильное подтверждение: {self.login} — OK")
        await self._poll_recovery(params)
        await self._verify_recovery_code(params)
        await self._get_next_step(params)
        key = await self._get_rsa_key()
        enc_old = self._encrypt(self.current_password, key["publickey_mod"], key["publickey_exp"])
        await self._verify_old_password(params, enc_old, key["timestamp"])
        logger.info(f"[ASRplus] Старый пароль подтверждён: {self.login}")
        await self._check_password_available(new_password)
        key2 = await self._get_rsa_key()
        enc_new = self._encrypt(new_password, key2["publickey_mod"], key2["publickey_exp"])
        await self._do_change_password(params, enc_new, key2["timestamp"])
        logger.info(f"[ASRplus] Пароль изменён: {self.login}")
        return new_password

    async def _login_steam(self):
        for attempt in range(3):
            try:
                await SteamGuard.sync_time_async()
                secs_left = SteamGuard._seconds_until_next_window()
                if secs_left < 10:
                    wait = secs_left + 3
                    logger.info(f"[ASRplus] Смена пароля: {self.login} — ожидание TOTP ({wait}с)")
                    await asyncio.sleep(wait)
                    await SteamGuard.sync_time_async()
                await self._steam.login_to_steam()
                logger.info(f"[ASRplus] Авторизация: {self.login} — OK")
                await asyncio.sleep(2)
                for wu in (f"{self.HELP}/en/", "https://steamcommunity.com/my/"):
                    try:
                        await self._steam.raw_request("GET", wu, headers={"User-Agent": self.UA})
                        logger.debug(f"[ASRplus] Warmup OK: {wu}")
                    except Exception as e:
                        logger.debug(f"[ASRplus] Warmup failed {wu}: {e}")
                return
            except Exception as e:
                err = str(e)
                logger.warning(f"[ASRplus] Авторизация попытка {attempt+1}/3: {err[:120]}")
                if "TwoFactorCodeMismatch" in err:
                    wait = SteamGuard._seconds_until_next_window() + 3
                    await asyncio.sleep(wait)
                    await SteamGuard.sync_time_async()
                elif "RateLimitExceeded" in err:
                    await asyncio.sleep(30 * (attempt + 1))
                elif "InvalidPassword" in err:
                    raise Exception(f"Неверный пароль для {self.login}")
                else:
                    if attempt >= 2:
                        raise
                    await asyncio.sleep(5)
        raise Exception(f"Steam login failed после 3 попыток для {self.login}")

    async def _get_wizard_params(self) -> PasswordChangeParams:
        urls = [
            f"{self.HELP}/wizard/HelpChangePassword?redir=store/account/",
            f"{self.HELP}/en/wizard/HelpChangePassword",
        ]
        for url in urls:
            try:
                resp = await self._steam.raw_request(
                    "GET", url,
                    headers={
                        "Accept": "text/html,*/*",
                        "Referer": "https://store.steampowered.com/",
                        "User-Agent": self.UA,
                    },
                    allow_redirects=True
                )
                final_url = ""
                if hasattr(resp, 'url'):
                    final_url = str(resp.url)
                elif hasattr(resp, 'real_url'):
                    final_url = str(resp.real_url)
                history = getattr(resp, "history", []) or []
                logger.debug(f"[ASRplus] WizardParams {url[:55]} -> {final_url[:100]} history={len(history)}")
                all_urls = [final_url] + [str(getattr(h, "url", "")) for h in history]
                for src in all_urls:
                    if "s=" in src and "issueid=" in src:
                        try:
                            q = dict(YarlURL(src).query)
                            if all(k in q for k in ("s", "account", "reset", "issueid")):
                                logger.debug(f"[ASRplus] Params from URL: {q}")
                                return PasswordChangeParams(**q)
                        except Exception as e:
                            logger.debug(f"[ASRplus] URL parse error: {e}")
                try:
                    if hasattr(resp, 'text') and callable(resp.text):
                        html_body = await resp.text()
                    elif isinstance(resp, bytes):
                        html_body = resp.decode("utf-8", errors="replace")
                    elif isinstance(resp, str):
                        html_body = resp
                    else:
                        html_body = ""
                except Exception as e:
                    logger.debug(f"[ASRplus] Read body error: {e}")
                    html_body = ""
                found = {}
                patterns = {
                    "s": [r'[?&]s=(\d+)', r'"s"\s*:\s*(\d+)'],
                    "account": [r'[?&]account=(\d+)', r'"account"\s*:\s*(\d+)'],
                    "reset": [r'[?&]reset=(\d+)', r'"reset"\s*:\s*(\d+)'],
                    "issueid": [r'[?&]issueid=(\d+)', r'"issueid"\s*:\s*(\d+)'],
                }
                for key_name, pats in patterns.items():
                    for pat in pats:
                        m = re.search(pat, html_body)
                        if m:
                            found[key_name] = m.group(1)
                            break
                if all(k in found for k in ("s", "account", "reset", "issueid")):
                    logger.debug(f"[ASRplus] Params from HTML: {found}")
                    return PasswordChangeParams(**found)
            except Exception as e:
                logger.warning(f"[ASRplus] WizardParams URL failed {url}: {e}")
        raise Exception(f"Не удалось получить wizard params для {self.login}")

    async def _playwright_open_wizard(self, params: PasswordChangeParams):
        wizard_url = (
            f"{self.HELP}/en/wizard/HelpWithLoginInfoEnterCode"
            f"?s={params.s}&account={params.account}&reset={params.reset}"
            f"&lost={params.lost}&issueid={params.issueid}"
        )
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("[ASRplus] Playwright не установлен — продолжаем без браузера")
            return
        cookies_for_pw = []
        for domain in ["help.steampowered.com", "store.steampowered.com", "steamcommunity.com"]:
            try:
                dc = await self._steam.cookies(domain)
                if isinstance(dc, dict):
                    for name, value in dc.items():
                        cookies_for_pw.append({
                            "name": name,
                            "value": str(value),
                            "domain": f".{domain}",
                            "path": "/"
                        })
            except Exception as e:
                logger.debug(f"[ASRplus] cookies {domain}: {e}")
        pw = None
        browser = None
        try:
            pw = await async_playwright().start()
            try:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--single-process",
                        "--no-zygote",
                        "--disable-extensions",
                        "--disable-software-rasterizer",
                        "--disable-background-networking",
                    ]
                )
            except Exception as e:
                logger.warning(f"[ASRplus] Chromium не запустился: {e} — продолжаем без браузера")
                return
            context = await browser.new_context(
                user_agent=self.UA,
                locale="en-US",
                viewport={"width": 1280, "height": 720}
            )
            if cookies_for_pw:
                await context.add_cookies(cookies_for_pw)
            page = await context.new_page()
            try:
                await page.goto(wizard_url, wait_until="domcontentloaded", timeout=30000)
                logger.info(f"[ASRplus] Playwright: wizard загружен")
            except Exception as e:
                logger.debug(f"[ASRplus] Playwright wizard goto: {e}")
            await asyncio.sleep(3)
        except Exception as e:
            logger.warning(f"[ASRplus] Playwright ошибка (non-critical): {e} — продолжаем без браузера")
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass

    async def _confirm_recovery(self, params: PasswordChangeParams) -> bool:
        cid_str = str(params.s)
        empty_in_a_row = 0
        for attempt in range(20):
            try:
                await SteamGuard.sync_time_async()
                ts = int(time.time()) + SteamGuard._time_offset
                conf_key = _generate_confirmation_key(self.identity_secret, ts, "getlist")
                getlist_url = (
                    "https://steamcommunity.com/mobileconf/getlist"
                    f"?p={urllib.parse.quote(self.device_id)}"
                    f"&a={self.steamid}"
                    f"&k={urllib.parse.quote(conf_key)}"
                    f"&t={ts}&m=android&tag=getlist"
                )
                try:
                    raw = await self._steam.raw_request(
                        "GET", getlist_url,
                        headers={
                            "Accept": "application/json, text/plain, */*",
                            "User-Agent": self.UA,
                            "X-Requested-With": "com.valvesoftware.android.steam.community",
                        }
                    )
                except Exception as e:
                    logger.warning(f"[ASRplus] getlist request error: {e}")
                    await asyncio.sleep(3)
                    continue
                try:
                    data = await self._parse_response(raw, getlist_url)
                except Exception as e:
                    logger.warning(f"[ASRplus] getlist parse: {e}")
                    await asyncio.sleep(3)
                    continue
                if not data.get("success"):
                    logger.warning(
                        f"[ASRplus] getlist not success "
                        f"(login={self.login}, sid={self.steamid}, dev={self.device_id[:12]}..): {data}"
                    )
                    await asyncio.sleep(3)
                    continue
                confs = data.get("conf", [])
                logger.info(
                    f"[ASRplus] getlist attempt {attempt+1}/20: "
                    f"{len(confs)} confirmation(s) for {self.login}"
                )
                if not confs:
                    empty_in_a_row += 1
                    if empty_in_a_row == 3:
                        logger.warning(
                            f"[ASRplus] {self.login}: {empty_in_a_row} пустых getlist подряд. "
                            "Возможные причины: 1) IP бота не доверен Steam — войди в Steam с этого IP и подтверди письмом; "
                            "2) device_id в maFile неверный; 3) confirmation уже была отклонена."
                        )
                        if tg_logs:
                            tg_logs.error(
                                f"⚠️ {self.login}: Steam не выдаёт подтверждение смены пароля.\n"
                                "Проверьте: 1) IP бота (нужен trusted для этого аккаунта), "
                                "2) device_id в maFile, 3) не заблокирован ли аккаунт."
                            )
                    await asyncio.sleep(3)
                    continue
                empty_in_a_row = 0
                for ci in confs:
                    logger.debug(
                        f"[ASRplus]   conf id={ci.get('id')} type={ci.get('type')} "
                        f"type_name={ci.get('type_name')} creator_id={ci.get('creator_id')} "
                        f"summary={ci.get('summary')}"
                    )
                target = next(
                    (ci for ci in confs if str(ci.get("creator_id", "")) == cid_str),
                    None
                )
                if target is None:
                    for ci in confs:
                        type_id = int(ci.get("type", 0))
                        type_name = str(ci.get("type_name", "")).lower()
                        summary = str(ci.get("summary", "")).lower()
                        if type_id == 6 or any(x in type_name for x in ("recovery", "password", "account")) \
                                or any(x in summary for x in ("recovery", "password", "change")):
                            target = ci
                            logger.debug(f"[ASRplus] fallback by type/summary: {type_name!r} {summary!r}")
                            break
                if target is None and len(confs) == 1:
                    target = confs[0]
                    logger.debug(f"[ASRplus] fallback: единственная confirmation (creator_id={confs[0].get('creator_id')}, expected {cid_str})")
                if target is None:
                    logger.debug(
                        f"[ASRplus] attempt {attempt+1}: creator_id {cid_str} не найден среди "
                        f"{[ci.get('creator_id') for ci in confs]}"
                    )
                    await asyncio.sleep(3)
                    continue
                await asyncio.sleep(1)
                ts2 = int(time.time()) + SteamGuard._time_offset
                allow_key = _generate_confirmation_key(self.identity_secret, ts2, "allow")
                ajaxop_url = (
                    "https://steamcommunity.com/mobileconf/ajaxop"
                    f"?p={urllib.parse.quote(self.device_id)}"
                    f"&a={self.steamid}"
                    f"&k={urllib.parse.quote(allow_key)}"
                    f"&t={ts2}&m=android&tag=allow&op=allow"
                    f"&cid={target['id']}&ck={target['nonce']}"
                )
                try:
                    raw = await self._steam.raw_request(
                        "GET", ajaxop_url,
                        headers={
                            "Accept": "application/json, text/plain, */*",
                            "User-Agent": self.UA,
                            "X-Requested-With": "com.valvesoftware.android.steam.community",
                        }
                    )
                    result = await self._parse_response(raw, ajaxop_url)
                except Exception as e:
                    logger.warning(f"[ASRplus] ajaxop error: {e}")
                    await asyncio.sleep(3)
                    continue
                logger.info(f"[ASRplus] ajaxop result for {self.login}: {result}")
                if result.get("success"):
                    return True
                logger.error(f"[ASRplus] Подтверждение отклонено: {result}")
                return False
            except Exception as e:
                logger.warning(f"[ASRplus] Подтверждение попытка {attempt+1}: {e}")
                await asyncio.sleep(3)
        return False

    async def _get_sessionid(self) -> str:
        try:
            cookies = await self._steam.cookies("help.steampowered.com")
            if isinstance(cookies, dict) and "sessionid" in cookies:
                return cookies["sessionid"]
        except Exception:
            pass
        try:
            return await self._steam.sessionid("help.steampowered.com")
        except Exception:
            pass
        raise Exception("Не удалось получить sessionid для help.steampowered.com")

    async def _parse_response(self, resp, url: str) -> dict:
        if isinstance(resp, bytes):
            text = resp.decode("utf-8", errors="replace")
        elif isinstance(resp, str):
            text = resp
        elif hasattr(resp, 'text') and callable(resp.text):
            text = await resp.text()
        else:
            text = str(resp) if resp is not None else ""
        text = text.strip()
        if not text:
            raise Exception(f"Empty response from {url}")
        if text.startswith("<"):
            low = text.lower()
            if any(s in low for s in ("verify by email", "check your email", "email verification",
                                      "подтвердите по почте", "проверьте почту", "ссылку из письма")):
                raise SteamEmailVerificationRequired(
                    f"Steam требует email-подтверждение для recovery (URL: {url}). "
                    "Залогинься в Steam с этого IP и подтверди вход письмом, затем повтори."
                )
            m = re.search(r'<div[^>]*id=["\']error_description["\'][^>]*>([^<]+)<', text)
            err = m.group(1).strip() if m else text[:150]
            raise Exception(f"HTML response from {url}: {err}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise Exception(f"JSONDecodeError from {url}: {e} | raw: {text[:200]}")

    async def _help_post(self, endpoint: str, data: dict) -> dict:
        url = f"{self.HELP}{endpoint}"
        sid = await self._get_sessionid()
        data["sessionid"] = sid
        try:
            resp = await self._steam.raw_request(
                "POST", url,
                data=data,
                headers={
                    "Accept": "*/*",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": self.HELP,
                    "Referer": f"{self.HELP}/en/",
                    "User-Agent": self.UA,
                    "X-Requested-With": "XMLHttpRequest",
                }
            )
        except Exception as e:
            raise Exception(f"POST {endpoint} failed: {e}")
        return await self._parse_response(resp, url)

    async def _help_get(self, endpoint: str, params: dict) -> dict:
        sid = await self._get_sessionid()
        params["sessionid"] = sid
        qs = urllib.parse.urlencode(params)
        url = f"{self.HELP}{endpoint}?{qs}"
        try:
            resp = await self._steam.raw_request(
                "GET", url,
                headers={
                    "Accept": "*/*",
                    "User-Agent": self.UA,
                    "X-Requested-With": "XMLHttpRequest",
                }
            )
        except Exception as e:
            raise Exception(f"GET {endpoint} failed: {e}")
        return await self._parse_response(resp, url)

    async def _poll_recovery(self, params: PasswordChangeParams):
        for i in range(15):
            r = await self._help_post(
                "/en/wizard/AjaxPollAccountRecoveryConfirmation",
                {
                    "wizard_ajax": "1",
                    "s": str(params.s),
                    "reset": str(params.reset),
                    "lost": str(params.lost),
                    "method": "8",
                    "issueid": str(params.issueid),
                    "gamepad": "0",
                }
            )
            logger.debug(f"[ASRplus] PollRecovery {i+1}: {r}")
            if r.get("success") or r.get("continue"):
                return
            if r.get("errorMsg"):
                raise Exception(f"PollRecovery: {r['errorMsg']}")
            await asyncio.sleep(2)
        raise Exception("Poll confirmation timed out")

    async def _verify_recovery_code(self, params: PasswordChangeParams):
        r = await self._help_get(
            "/en/wizard/AjaxVerifyAccountRecoveryCode",
            {
                "code": "",
                "s": str(params.s),
                "reset": str(params.reset),
                "lost": str(params.lost),
                "method": "8",
                "issueid": str(params.issueid),
                "wizard_ajax": "1",
                "gamepad": "0",
            }
        )
        logger.debug(f"[ASRplus] VerifyCode: {r}")
        if r.get("errorMsg"):
            raise Exception(f"VerifyCode: {r['errorMsg']}")

    async def _get_next_step(self, params: PasswordChangeParams):
        r = await self._help_post(
            "/en/wizard/AjaxAccountRecoveryGetNextStep",
            {
                "wizard_ajax": "1",
                "s": str(params.s),
                "account": str(params.account),
                "reset": str(params.reset),
                "issueid": str(params.issueid),
                "lost": "2",
            }
        )
        logger.debug(f"[ASRplus] GetNextStep: {r}")
        if r.get("errorMsg"):
            raise Exception(f"GetNextStep: {r['errorMsg']}")

    async def _get_rsa_key(self) -> dict:
        r = await self._help_post(
            "/en/login/getrsakey/",
            {"username": self.login}
        )
        logger.debug(f"[ASRplus] RSA: has_mod={bool(r.get('publickey_mod'))}")
        if not r.get("publickey_mod"):
            raise Exception(f"RSA key missing: {r}")
        return r

    async def _verify_old_password(self, params: PasswordChangeParams, enc_pwd: str, ts: str):
        r = await self._help_post(
            "/en/wizard/AjaxAccountRecoveryVerifyPassword/",
            {
                "s": str(params.s),
                "lost": "2",
                "reset": "1",
                "password": enc_pwd,
                "rsatimestamp": ts,
            }
        )
        logger.debug(f"[ASRplus] VerifyOldPwd: {r}")
        if r.get("errorMsg"):
            raise Exception(f"VerifyOldPassword: {r['errorMsg']}")

    async def _check_password_available(self, password: str):
        r = await self._help_post(
            "/en/wizard/AjaxCheckPasswordAvailable/",
            {
                "wizard_ajax": "1",
                "password": password,
            }
        )
        logger.debug(f"[ASRplus] CheckNewPwd: {r}")
        if not r.get("available"):
            raise Exception(f"Password not available: {r}")

    async def _do_change_password(self, params: PasswordChangeParams, enc_pwd: str, ts: str):
        r = await self._help_post(
            "/en/wizard/AjaxAccountRecoveryChangePassword/",
            {
                "wizard_ajax": "1",
                "s": str(params.s),
                "account": str(params.account),
                "password": enc_pwd,
                "rsatimestamp": ts,
            }
        )
        logger.debug(f"[ASRplus] DoChangePassword: {r}")
        if r.get("errorMsg"):
            raise Exception(f"ChangePassword error: {r['errorMsg']}")
        if not r.get("success") and not r.get("hash"):
            raise Exception(f"ChangePassword no success: {r}")

    @staticmethod
    def _encrypt(password: str, mod: str, exp: str) -> str:
        pk = rsa.PublicKey(n=int(mod, 16), e=int(exp, 16))
        return base64.b64encode(rsa.encrypt(password.encode("ascii"), pk)).decode()

async def change_password_async(mafile: dict, current_password: str) -> str:
    return await SteamPasswordChanger(mafile, current_password).change_password()

def change_password_sync(mafile: dict, current_password: str, acc_id: int = 0) -> str:
    lock = _get_acc_lock(acc_id) if acc_id else _pwd_change_lock
    with lock:
        result = [None]
        error = [None]
        loop_ref = [None]
        main_task_ref = [None]
        done_evt = threading.Event()
        async def _runner():
            main_task_ref[0] = asyncio.current_task()
            return await change_password_async(mafile, current_password)
        def _run():
            loop = asyncio.new_event_loop()
            loop_ref[0] = loop
            asyncio.set_event_loop(loop)
            try:
                result[0] = loop.run_until_complete(_runner())
            except asyncio.CancelledError as e:
                error[0] = Exception("Password change cancelled (timeout)")
            except Exception as e:
                error[0] = e
            finally:
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                try:
                    loop.close()
                except Exception:
                    pass
                done_evt.set()
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        if not done_evt.wait(timeout=PASSWORD_CHANGE_TIMEOUT):
            loop = loop_ref[0]
            task = main_task_ref[0]
            if loop and task:
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except Exception:
                    pass
            done_evt.wait(timeout=15)
            if t.is_alive():
                raise Exception("Password change timed out (worker did not stop)")
            raise Exception("Password change timed out")
        if error[0]:
            raise error[0]
        if result[0] is None:
            raise Exception("Password change returned no result")
        return result[0]

class LotConfig(BaseModel):
    tag: str
    extend_lot_id: Optional[str] = None
    # Список лотов-продлений с разным временем — для лотов типа "fixed",
    # чтобы покупатель мог выбрать, на сколько часов продлить аренду.
    # Каждый элемент: {"lot_id": "12345", "hours": 5.0}
    # Для лотов типа "hourly" по-прежнему используется старое поле extend_lot_id.
    extend_options: List[Dict[str, Any]] = Field(default_factory=list)
    note: Optional[str] = None  # пользовательская заметка/название лота (не влияет на логику)
    subcategory_id: Optional[int] = None  # для фильтра "наш раздел" при неопознанном лоте
    lot_type: str = "hourly"  # "hourly" (1 шт = 1 час) или "fixed" (фикс. время на лот)
    # Время аренды (в часах) для лотов типа "fixed". Задаётся один раз при
    # добавлении лота, но может быть изменено позже в настройках лота.
    # Именно это значение используется для выдачи времени аренды, когда
    # заказ относится к фиксированному лоту (quantity заказа игнорируется).
    fixed_hours: Optional[float] = None
    # Уникальный служебный ID лота.
    # В отличие от `tag` (который выбирает ПУЛ аккаунтов и может повторяться
    # у нескольких лотов), match_tag уникален для каждого лота, генерируется
    # автоматически и используется ТОЛЬКО для однозначного опознавания лота
    # по описанию заказа. Он никогда не совпадёт с номером заказа, т.к.
    # генерируется с префиксом и случайными буквами+цифрами.
    match_tag: Optional[str] = None
    class Config:
        extra = "allow"

class MessagesConfig(BaseModel):
    order_completed: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "🎮  ДАННЫЕ ОТ АККАУНТА\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "🔑  Логин:     $login\n"
        "🔒  Пароль:    $password\n"
        "⏳  Аренда:    $hours $hours_word\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "📲  Для входа нужен Steam Guard код\n"
        "▶️  Напишите  !код  чтобы получить его\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "📋  ДОСТУПНЫЕ КОМАНДЫ\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "$commands_list\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    guard_code: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "🔐  STEAM GUARD КОД\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "▸  Код:          $code\n"
        "▸  Действителен: ~30 сек\n"
        "▸  Аренда до:    $end_time\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    rent_over: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "🔴  АРЕНДА ЗАВЕРШЕНА\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Пароль от аккаунта изменён.\n"
        "Спасибо за использование сервиса!\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    warning: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "⚠️  ВНИМАНИЕ\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "До конца аренды осталось меньше 10 минут.\n"
        "Напишите команду  !продлить  чтобы продлить текущую аренду.\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    warning_multi: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "⚠️  ВНИМАНИЕ\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "До конца аренды осталось меньше 10 минут.\n"
        "У вас несколько активных аккаунтов — чтобы продлить нужный,\n"
        "перейдите по его ссылке:\n\n"
        "$accounts_list\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    extended: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "✅  АРЕНДА ПРОДЛЕНА\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "▸  Добавлено:  +$hours ч.\n"
        "▸  Аренда до:  $end_time\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    auto_extended: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "🔄  АВТО-ПРОДЛЕНИЕ\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "▸  Добавлено:  +$hours ч.\n"
        "▸  Аренда до:  $end_time\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    bonus: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "🎁  БОНУС ЗА ОТЗЫВ\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Вам начислено: +$hours ч. бесплатной аренды\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    time_info: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "⏱  ВРЕМЯ АРЕНДЫ\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "▸  Осталось:   $remaining\n"
        "▸  Аренда до:  $end_time\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    error_msg: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "❌  ОШИБКА\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Произошла техническая ошибка.\n"
        "Пожалуйста, ожидайте — продавец уже в курсе.\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    no_accounts: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "📭  НЕТ СВОБОДНЫХ АККАУНТОВ\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "К сожалению, все аккаунты заняты.\n"
        "Средства будут возвращены автоматически.\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    refunded: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "💰  ВОЗВРАТ СРЕДСТВ\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Средства успешно возвращены на ваш счёт.\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    rent_expired: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "⏰  ВРЕМЯ АРЕНДЫ ИСТЕКЛО\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Ваша аренда завершена по истечению времени.\n"
        "Спасибо за использование сервиса!\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    no_order: str        = "❌  Активный заказ не найден. Обратитесь к продавцу."
    no_account: str      = "❌  Аккаунт не найден. Обратитесь к продавцу."
    code_error: str      = "❌  Не удалось сгенерировать код. Попробуйте через 30 секунд."
    config_error: str    = "❌  Ошибка конфигурации. Обратитесь к продавцу."
    rent_not_started: str = "▶️  Напишите  !код  чтобы получить данные от аккаунта."
    extend_link: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "🔄  ПРОДЛЕНИЕ АРЕНДЫ\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Для продления оплатите лот по ссылке:\n"
        "$link\n\n"
        "▸  Осталось: $remaining\n"
        "⚠️  Ссылка активна 5 минут\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    extend_no_lot: str   = "❌  Лот для продления не настроен. Обратитесь к продавцу."
    stock_info: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "📦  ДОСТУПНО ДЛЯ АРЕНДЫ\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "$stock_list\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    stock_empty: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "📭  НЕТ АККАУНТОВ\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Сейчас все аккаунты заняты. Попробуйте позже.\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    seller_called: str = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "📞  ПРОДАВЕЦ ВЫЗВАН\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Продавцу отправлено уведомление.\n"
        "Пожалуйста, ожидайте ответа в этом чате.\n\n"
        "━━━━━━━━━━━━━━━━━━━"
    )
    seller_call_cooldown: str = (
        "⏳  Вы уже вызывали продавца недавно. Пожалуйста, подождите немного."
    )
    DESCRIPTIONS: ClassVar[Dict[str, str]] = {
        "order_completed":  "📋 Выдача данных",
        "guard_code":       "🔑 Steam Guard код",
        "rent_over":        "⛔ Конец аренды",
        "warning":          "⚠️ Предупреждение 10 мин",
        "warning_multi":    "⚠️ Предупреждение (неск. аккаунтов)",
        "extended":         "✅ Продление",
        "auto_extended":    "🔄 Авто-продление",
        "bonus":            "🎁 Бонус за отзыв",
        "time_info":        "⏱ Команда !time",
        "rent_expired":     "⏰ Время истекло",
        "error_msg":        "❌ Общая ошибка",
        "no_accounts":      "📭 Нет аккаунтов",
        "refunded":         "💰 Возврат",
        "no_order":         "🔍 Заказ не найден",
        "no_account":       "👤 Аккаунт не найден",
        "code_error":       "❌ Ошибка кода",
        "config_error":     "⚙️ Ошибка конфигурации",
        "rent_not_started": "⏳ Аренда не начата",
        "extend_link":      "🔗 Ссылка на продление",
        "extend_no_lot":    "❌ Лот не найден",
        "stock_info":       "📦 Наличие",
        "stock_empty":      "📭 Нет аккаунтов",
        "seller_called":    "📞 Продавец вызван",
        "seller_call_cooldown": "⏳ Кулдаун вызова продавца",
    }
    class Config:
        extra = "allow"

class ReviewRule(BaseModel):
    rent_hours: int
    bonus_hours: float
    class Config:
        extra = "allow"

class AccountModel(BaseModel):
    id: int
    login: str
    password: str
    mafile: Dict[str, Any]
    tag: str = "default"
    status: str = RentStatus.FREE
    current_order: Optional[str] = None
    rental_end: Optional[str] = None
    owner: Optional[str] = None
    owner_id: Optional[int] = None
    owner_chat_id: Optional[Any] = None
    rental_start: Optional[str] = None
    access_count: int = 0
    time_limit_hours: Optional[float] = None
    class Config:
        extra = "allow"

@dataclass
class RentOrder:
    id: str
    chat_id: Optional[int]
    buyer: str
    buyer_id: int
    acc_id: int
    acc_login: str
    acc_tag: str
    hours: float
    status: str = RentStatus.ACTIVE
    warned: bool = False
    review_claimed: bool = False
    created_at: str = field(default_factory=lambda: _fmt(_now()))
    is_extension: bool = False
    lot_id: Optional[str] = None
    # True если покупатель подтвердил получение заказа на FunPay (статус CLOSED),
    # но аренда ещё активна. НЕ влияет на RentStatus — команды !код/!time/!аккаунт
    # должны продолжать работать до истечения фактического времени аренды.
    buyer_confirmed: bool = False

    def __post_init__(self):
        if self.chat_id is not None:
            try:
                self.chat_id = int(self.chat_id)
            except (TypeError, ValueError):
                self.chat_id = None

    def update(self, **kwargs):
        with _data_lock:
            for k, v in kwargs.items():
                setattr(self, k, v)
            try:
                _save_orders()
            except Exception:
                pass

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "id", "chat_id", "buyer", "buyer_id", "acc_id", "acc_login", "acc_tag",
            "hours", "status", "warned", "review_claimed", "created_at",
            "is_extension", "lot_id", "buyer_confirmed")}

class Settings(BaseModel):
    enabled: bool = False
    autoback_on_error: bool = False
    auto_extend: bool = False
    auto_disable_lots: bool = False
    auto_enable_lots: bool = False
    auto_free_on_error: bool = False
    save_deleted_acc: bool = True
    lots: Dict[str, Any] = {}
    # Продление "по тегу" (общее для всех лотов с этим тегом), чтобы не
    # привязывать лот-продление к каждому лоту отдельно:
    # {tag: {"extend_lot_id": Optional[str], "extend_options": [{"lot_id","hours"}, ...]}}
    tag_extend: Dict[str, Any] = {}
    review_rules: List[Dict[str, Any]] = [
        {"rent_hours": 3, "bonus_hours": 1.0}, {"rent_hours": 6, "bonus_hours": 2.0},
        {"rent_hours": 12, "bonus_hours": 4.0}, {"rent_hours": 24, "bonus_hours": 6.0},
        {"rent_hours": 72, "bonus_hours": 12.0}, {"rent_hours": 168, "bonus_hours": 24.0},
    ]
    messages: MessagesConfig = MessagesConfig()
    notification_order_completed: bool = True
    notification_error: bool = True
    notification_refund: bool = True
    notification_preparing: bool = True
    blacklist: List[str] = []
    class Config:
        extra = "allow"

    def add_to_blacklist(self, username: str):
        uname = username.strip().lower()
        if uname not in self.blacklist:
            self.blacklist.append(uname)
            _save_settings()

    def remove_from_blacklist(self, username: str):
        uname = username.strip().lower()
        if uname in self.blacklist:
            self.blacklist.remove(uname)
            _save_settings()

    def is_blacklisted(self, username: str) -> bool:
        return (username or "").strip().lower() in self.blacklist

    def toggle(self, p):
        setattr(self, p, not getattr(self, p))
        _save_settings()

    def set_message(self, k, v):
        setattr(self.messages, k, v)
        _save_settings()

    def has_lot(self, lot_id) -> bool:
        return str(lot_id) in self.lots

    def get_lot(self, lot_id: str) -> Optional[LotConfig]:
        raw = self.lots.get(str(lot_id))
        if raw is None:
            return None
        if isinstance(raw, str):
            return LotConfig(tag=_ntag(raw))
        if isinstance(raw, dict):
            return LotConfig(tag=_ntag(raw.get("tag", "default")),
                             extend_lot_id=raw.get("extend_lot_id"),
                             extend_options=raw.get("extend_options") or [],
                             note=raw.get("note"),
                             subcategory_id=raw.get("subcategory_id"),
                             lot_type=raw.get("lot_type", "hourly"),
                             fixed_hours=raw.get("fixed_hours"),
                             match_tag=raw.get("match_tag"))
        return None

    def set_lot(self, lot_id: str, tag: str, extend_lot_id: Optional[str] = None,
                note: Optional[str] = None, subcategory_id: Optional[int] = None,
                lot_type: Optional[str] = None, fixed_hours: Optional[float] = None):
        existing = self.lots.get(str(lot_id), {})
        if isinstance(existing, str):
            existing = {"tag": _ntag(existing)}
        existing["tag"] = _ntag(tag)
        if extend_lot_id is not None:
            existing["extend_lot_id"] = str(extend_lot_id) if extend_lot_id else None
        if note is not None:
            existing["note"] = note.strip() if note.strip() else None
        if subcategory_id is not None:
            existing["subcategory_id"] = subcategory_id
        if lot_type is not None:
            existing["lot_type"] = lot_type
        elif "lot_type" not in existing:
            existing["lot_type"] = "hourly"
        if fixed_hours is not None:
            existing["fixed_hours"] = fixed_hours
        self.lots[str(lot_id)] = existing
        _save_settings()

    def add_lot_extend_option(self, lot_id: str, ext_lot_id: str, hours: float):
        """Добавляет/обновляет вариант лота-продления с конкретным временем
        (для лотов типа 'fixed', когда таких вариантов может быть несколько)."""
        existing = self.lots.get(str(lot_id), {})
        if isinstance(existing, str):
            existing = {"tag": _ntag(existing)}
        opts = list(existing.get("extend_options") or [])
        ext_lot_id = str(ext_lot_id)
        opts = [o for o in opts if str(o.get("lot_id")) != ext_lot_id]
        opts.append({"lot_id": ext_lot_id, "hours": float(hours)})
        opts.sort(key=lambda o: float(o.get("hours", 0)))
        existing["extend_options"] = opts
        self.lots[str(lot_id)] = existing
        _save_settings()

    def remove_lot_extend_option(self, lot_id: str, ext_lot_id: str):
        existing = self.lots.get(str(lot_id), {})
        if isinstance(existing, str):
            return
        opts = list(existing.get("extend_options") or [])
        opts = [o for o in opts if str(o.get("lot_id")) != str(ext_lot_id)]
        existing["extend_options"] = opts
        self.lots[str(lot_id)] = existing
        _save_settings()

    # ---- Продление "по тегу" (общее на всю категорию/тег) ----

    def get_tag_extend_lot_id(self, tag: str) -> Optional[str]:
        return (self.tag_extend.get(_ntag(tag)) or {}).get("extend_lot_id")

    def get_tag_extend_options(self, tag: str) -> List[Dict[str, Any]]:
        return list((self.tag_extend.get(_ntag(tag)) or {}).get("extend_options") or [])

    def set_tag_extend_lot_id(self, tag: str, ext_lot_id: Optional[str]):
        tag = _ntag(tag)
        existing = self.tag_extend.get(tag, {})
        existing["extend_lot_id"] = str(ext_lot_id) if ext_lot_id else None
        self.tag_extend[tag] = existing
        _save_settings()

    def add_tag_extend_option(self, tag: str, ext_lot_id: str, hours: float):
        tag = _ntag(tag)
        existing = self.tag_extend.get(tag, {})
        opts = list(existing.get("extend_options") or [])
        ext_lot_id = str(ext_lot_id)
        opts = [o for o in opts if str(o.get("lot_id")) != ext_lot_id]
        opts.append({"lot_id": ext_lot_id, "hours": float(hours)})
        opts.sort(key=lambda o: float(o.get("hours", 0)))
        existing["extend_options"] = opts
        self.tag_extend[tag] = existing
        _save_settings()

    def remove_tag_extend_option(self, tag: str, ext_lot_id: str):
        tag = _ntag(tag)
        existing = self.tag_extend.get(tag, {})
        opts = list(existing.get("extend_options") or [])
        opts = [o for o in opts if str(o.get("lot_id")) != str(ext_lot_id)]
        existing["extend_options"] = opts
        self.tag_extend[tag] = existing
        _save_settings()

    def tags_with_lots(self) -> List[str]:
        """Все теги, на которые ссылается хотя бы один настроенный лот."""
        seen = []
        for lid in self.lots:
            lc = self.get_lot(lid)
            if lc and lc.tag and lc.tag not in seen:
                seen.append(lc.tag)
        return seen

    def set_lot_fixed_hours(self, lot_id: str, hours: float):
        """Задать/изменить фиксированное время аренды (в часах) для лота типа 'fixed'."""
        existing = self.lots.get(str(lot_id), {})
        if isinstance(existing, str):
            existing = {"tag": _ntag(existing)}
        existing["fixed_hours"] = hours
        self.lots[str(lot_id)] = existing
        _save_settings()

    def set_lot_note(self, lot_id: str, note: str):
        existing = self.lots.get(str(lot_id), {})
        if isinstance(existing, str):
            existing = {"tag": _ntag(existing)}
        existing["note"] = note.strip() if note.strip() else None
        self.lots[str(lot_id)] = existing
        _save_settings()

    def del_lot(self, lot_id: str):
        self.lots.pop(str(lot_id), None)
        _save_settings()

    def rename_lot(self, old_id: str, new_id: str) -> bool:
        old_id, new_id = str(old_id), str(new_id)
        if old_id not in self.lots or new_id == old_id:
            return False
        self.lots[new_id] = self.lots.pop(old_id)
        _save_settings()
        return True

    def find_lot_id_by_tag(self, tag: str) -> Optional[str]:
        tag = _ntag(tag)
        for lid in self.lots:
            lc = self.get_lot(lid)
            if lc and _ntag(lc.tag) == tag:
                return lid
        return None

    def find_main_lot_by_configured_extend_id(self, raw_offer_id: str) -> Optional[str]:
        """Ищет основной лот по СТРУКТУРНОЙ привязке (extend_lot_id /
        extend_options[].lot_id в настройках плагина), а не по тексту в
        описании лота на FunPay. Это приоритетный и надёжный способ:
        текстовый Auto ID (match_tag), вписанный в описание лота-продления
        на FunPay, может устареть или быть перезаписан вручную (например,
        при переиспользовании физического лота FunPay под другой основной
        лот), а структурная привязка в настройках бота — нет. Проверяем
        также привязки "продление по тегам" (общие на весь тег)."""
        if not raw_offer_id:
            return None
        target = _ntag(raw_offer_id)
        for lid in self.lots:
            lc = self.get_lot(lid)
            if not lc:
                continue
            if lc.extend_lot_id and _ntag(lc.extend_lot_id) == target:
                return lid
            for opt in (lc.extend_options or []):
                if _ntag(str(opt.get("lot_id"))) == target:
                    return lid
        # Общие лоты-продления по тегу (без индивидуальной привязки к лоту)
        for tag, data in list(getattr(self, "tag_extend", {}) or {}).items():
            data = data or {}
            ext_id = data.get("extend_lot_id")
            if ext_id and _ntag(str(ext_id)) == target:
                found = self.find_lot_id_by_tag(tag)
                if found:
                    return found
            for opt in (data.get("extend_options") or []):
                if _ntag(str(opt.get("lot_id"))) == target:
                    found = self.find_lot_id_by_tag(tag)
                    if found:
                        return found
        return None

    def find_lot_id_by_match_tag(self, match_tag: str) -> Optional[str]:
        """Ищет лот по уникальному служебному ID."""
        mt = _ntag(match_tag)
        for lid in self.lots:
            lc = self.get_lot(lid)
            if lc and lc.match_tag and _ntag(lc.match_tag) == mt:
                return lid
        return None

    def ensure_match_tag(self, lot_id: str) -> Optional[str]:
        """
        Гарантирует, что у лота lot_id есть уникальный служебный ID
        (match_tag, например "id7k2p9a"). Если его ещё нет — генерирует
        и сохраняет. Возвращает тег (без #).
        """
        lot_id = str(lot_id)
        lc = self.get_lot(lot_id)
        if lc is None:
            return None
        if lc.match_tag:
            return _ntag(lc.match_tag)
        existing = {
            _ntag(c.match_tag) for c in
            (self.get_lot(lid) for lid in list(self.lots.keys()))
            if c and c.match_tag
        }
        new_tag = _gen_match_tag(existing)
        raw = self.lots.get(lot_id, {})
        if isinstance(raw, str):
            raw = {"tag": _ntag(raw)}
        raw["match_tag"] = new_tag
        self.lots[lot_id] = raw
        _save_settings()
        return new_tag

    def ensure_all_match_tags(self) -> None:
        """Гарантирует, что у ВСЕХ настроенных лотов есть ID."""
        for lid in list(self.lots.keys()):
            self.ensure_match_tag(lid)

    def get_review_rules(self) -> List[ReviewRule]:
        return sorted([ReviewRule(**r) for r in self.review_rules if isinstance(r, dict)],
                      key=lambda x: x.rent_hours)

    def add_review_rule(self, rent_hours: int, bonus_hours: float):
        self.review_rules = [r for r in self.review_rules
                             if not (isinstance(r, dict) and r.get("rent_hours") == rent_hours)]
        self.review_rules.append({"rent_hours": rent_hours, "bonus_hours": bonus_hours})
        _save_settings()

    def del_review_rule(self, rent_hours: int):
        self.review_rules = [r for r in self.review_rules
                             if not (isinstance(r, dict) and r.get("rent_hours") == rent_hours)]
        _save_settings()

    def get_bonus_for_hours(self, hours: float) -> float:
        bonus = 0.0
        for rule in self.get_review_rules():
            if hours >= rule.rent_hours:
                bonus = rule.bonus_hours
        return bonus

SETTINGS: Optional[Settings] = None
ACCOUNTS: List[AccountModel] = []
ORDERS: Dict[str, RentOrder] = {}
PWD_BACKUPS: Dict[str, Dict[str, Any]] = {}
PWD_BACKUP_LIMIT = 10
PWD_BACKUP_HUMAN_LIMIT = 20  # вручную заданные пароли тоже не хранятся бесконечно
_pwd_backup_lock = threading.Lock()
cardinal_ref: Optional[Cardinal] = None
tg_logs: Optional[Any] = None

_code_cooldowns: Dict[str, float] = {}
_cooldowns_lock = threading.Lock()
_processed_orders: Dict[str, float] = {}
_temp_storage: Dict[int, dict] = {}
_tag_queue_index: Dict[str, int] = {}
_tag_queue_lock = threading.Lock()
_data_lock = threading.RLock()  # RLock: AccountRepo-методы могут вызывать друг друга из одного потока
_processed_lock = threading.Lock()
_toggling_tags: Set[str] = set()
_toggling_lock = threading.Lock()
_stop_event = threading.Event()

# Заказы без тега — игнорируются полностью, не сохраняются в плагине
# {order_id: timestamp} — через IGNORED_ORDER_TTL секунд запись удаляется
_ignored_orders: Dict[str, float] = {}
_ignored_lock = threading.Lock()
IGNORED_ORDER_TTL = 1800  # 30 минут — потом запись вылетает сама

# ---------------------------------------------------------------------------
# PendingOrderStore — хранилище входящих заказов до подтверждения покупателем
# ---------------------------------------------------------------------------
@dataclass
class PendingOrder:
    order_id: str
    buyer: str
    buyer_id: int
    chat_id: Any
    tag: str
    lot_id: Optional[str]
    hours: int
    received_at: float          # time.time() момент поступления
    confirmed: bool = False     # True после того как покупатель написал !код / !аккаунт
    confirmed_at: Optional[float] = None
    ttl: float = 7200.0        # сколько хранить (сек). По умолчанию 2 часа

    def age_str(self) -> str:
        sec = int(time.time() - self.received_at)
        h, m = divmod(sec, 3600)
        return f"{h}ч {m // 60}м" if h else f"{m // 60}м {sec % 60}с"

    def is_expired(self) -> bool:
        return time.time() - self.received_at > self.ttl

class PendingOrderStore:
    """
    Хранит заказы, ожидающие первого контакта покупателя.
    - Заказ добавляется сразу после выдачи аккаунта (в _assign_account).
    - При первой команде !код / !аккаунт от покупателя — отмечается confirmed.
    - По истечении TTL запись удаляется (в cleanup).
    - Telegram-бот может показывать список ожидающих подтверждения.
    """
    def __init__(self):
        self._store: Dict[str, PendingOrder] = {}
        self._lock = threading.Lock()

    def add(self, order_id: str, buyer: str, buyer_id: int, chat_id: Any,
            tag: str, lot_id: Optional[str], hours: int, ttl: float = 7200.0):
        with self._lock:
            self._store[order_id] = PendingOrder(
                order_id=order_id, buyer=buyer, buyer_id=buyer_id,
                chat_id=chat_id, tag=tag, lot_id=lot_id, hours=hours,
                received_at=time.time(), ttl=ttl
            )

    def confirm(self, order_id: str):
        with self._lock:
            p = self._store.get(order_id)
            if p and not p.confirmed:
                p.confirmed = True
                p.confirmed_at = time.time()

    def confirm_by_buyer(self, buyer_id: int, buyer_name: str = "") -> Optional[str]:
        """Ищет незакрытый заказ покупателя и отмечает confirmed. Возвращает order_id."""
        buyer_name_l = (buyer_name or "").strip().lower()
        with self._lock:
            for p in self._store.values():
                if p.confirmed:
                    continue
                if (buyer_id and p.buyer_id == buyer_id) or \
                   (buyer_name_l and p.buyer.strip().lower() == buyer_name_l):
                    p.confirmed = True
                    p.confirmed_at = time.time()
                    return p.order_id
        return None

    def get(self, order_id: str) -> Optional[PendingOrder]:
        with self._lock:
            return self._store.get(order_id)

    def get_by_buyer(self, buyer_id: int, buyer_name: str = "") -> Optional[PendingOrder]:
        buyer_name_l = (buyer_name or "").strip().lower()
        with self._lock:
            for p in self._store.values():
                if (buyer_id and p.buyer_id == buyer_id) or \
                   (buyer_name_l and p.buyer.strip().lower() == buyer_name_l):
                    return p
        return None

    def remove(self, order_id: str):
        with self._lock:
            self._store.pop(order_id, None)

    def cleanup_expired(self) -> int:
        """Удаляет истёкшие записи. Возвращает кол-во удалённых."""
        with self._lock:
            expired = [k for k, p in self._store.items() if p.is_expired()]
            for k in expired:
                del self._store[k]
        return len(expired)

    def all_pending(self) -> List[PendingOrder]:
        """Все незакрытые (не confirmed) заказы."""
        with self._lock:
            return [p for p in self._store.values() if not p.confirmed]

    def all_unconfirmed_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._store.values() if not p.confirmed)

    def snapshot(self) -> List[PendingOrder]:
        with self._lock:
            return list(self._store.values())

_pending_store = PendingOrderStore()
# ---------------------------------------------------------------------------

def _save_settings():
    _save_json("settings", SETTINGS.dict())

def _save_accounts():
    _save_json("accounts", [a.dict() for a in ACCOUNTS])

def _save_pwd_backups():
    _save_json("pwd_backups", PWD_BACKUPS)

def _record_password_backup(acc_id: int, login: str, password: str, source: str):
    """Сохраняет пароль в бэкап.
    source == "bot"   — пароль изменён автоматически/ботом (хранятся только последние PWD_BACKUP_LIMIT шт.)
    source == "human" — пароль задан вручную администратором (хранятся последние PWD_BACKUP_HUMAN_LIMIT шт.)
    """
    if not password:
        return
    with _pwd_backup_lock:
        key = str(acc_id)
        entry = PWD_BACKUPS.setdefault(key, {"login": login, "bot": [], "human": []})
        entry["login"] = login
        rec = {"password": password, "changed_at": _fmt(_now())}
        if source == "bot":
            lst = entry.setdefault("bot", [])
            lst.append(rec)
            if len(lst) > PWD_BACKUP_LIMIT:
                del lst[: len(lst) - PWD_BACKUP_LIMIT]
        else:
            lst = entry.setdefault("human", [])
            lst.append(rec)
            if len(lst) > PWD_BACKUP_HUMAN_LIMIT:
                del lst[: len(lst) - PWD_BACKUP_HUMAN_LIMIT]
        _save_pwd_backups()

def _save_orders():
    _save_json("orders", {k: v.to_dict() for k, v in ORDERS.items()})

def _cleanup_orders():
    removed = []
    with _data_lock:
        if len(ORDERS) <= MAX_ORDERS_STORED:
            return
        cutoff_dt = _now() - timedelta(days=ORDERS_MAX_AGE_DAYS)
        cutoff = _fmt(cutoff_dt)
        to_remove = [k for k, o in ORDERS.items()
                     if o.status in (RentStatus.FINISHED, RentStatus.REFUND) and o.created_at < cutoff]
        for k in to_remove:
            del ORDERS[k]
            removed.append(k)
        if len(ORDERS) > MAX_ORDERS_STORED:
            finished = sorted(
                [(k, o) for k, o in ORDERS.items() if o.status in (RentStatus.FINISHED, RentStatus.REFUND)],
                key=lambda x: x[1].created_at)
            while len(ORDERS) > MAX_ORDERS_STORED and finished:
                k, _ = finished.pop(0)
                del ORDERS[k]
                removed.append(k)
        _save_orders()
    with _processed_lock:
        for k in removed:
            _processed_orders.pop(k, None)

def _cleanup_processed():
    with _processed_lock:
        now = time.time()
        to_remove = [oid for oid, ts in _processed_orders.items() if now - ts > 3600]
        for oid in to_remove:
            del _processed_orders[oid]
        if len(_processed_orders) > MAX_PROCESSED_IDS:
            sorted_items = sorted(_processed_orders.items(), key=lambda x: x[1])
            to_remove = [oid for oid, _ in sorted_items[:len(_processed_orders)//2]]
            for oid in to_remove:
                del _processed_orders[oid]

def _cleanup_cooldowns():
    now_ts = time.time()
    with _cooldowns_lock:
        stale_after = max(CODE_COOLDOWN, SELLER_CALL_COOLDOWN) * 2
        stale = [k for k, v in _code_cooldowns.items() if now_ts - v > stale_after]
        for k in stale:
            del _code_cooldowns[k]
    # Очистка игнорируемых заказов по TTL — они вылетают и не сохраняются
    with _ignored_lock:
        stale_ignored = [k for k, v in _ignored_orders.items() if now_ts - v > IGNORED_ORDER_TTL]
        for k in stale_ignored:
            del _ignored_orders[k]
        if stale_ignored:
            logger.debug(f"[ASRplus] Очищено {len(stale_ignored)} устаревших игнорируемых заказов")

def _load_all():
    global SETTINGS, ACCOUNTS, ORDERS, PWD_BACKUPS
    raw = _load_json("settings")
    if "review_rules" in raw and isinstance(raw["review_rules"], dict):
        raw["review_rules"] = [{"rent_hours": int(k), "bonus_hours": v}
                                for k, v in raw["review_rules"].items()]
    SETTINGS = Settings(**raw)
    changed = False
    for lid, val in list(SETTINGS.lots.items()):
        if isinstance(val, str):
            SETTINGS.lots[lid] = {"tag": _ntag(val)}
            changed = True
        elif isinstance(val, dict):
            val.pop("count", None)
            val.pop("hours", None)
            if "tag" not in val:
                val["tag"] = "default"
            changed = True
    if changed:
        _save_settings()
    d = _load_json("accounts")
    if isinstance(d, list):
        for a in d:
            a.pop("allowed_hours", None)
            a.pop("rent_hours", None)
        ACCOUNTS = [AccountModel(**a) for a in d]
    else:
        ACCOUNTS = []
    d = _load_json("orders")
    if isinstance(d, dict):
        for k, v in d.items():
            v.pop("acc_ids", None)
            v.pop("is_multi", None)
            v.setdefault("is_extension", False)
            v.setdefault("lot_id", None)
            v.setdefault("acc_login", "")
            v.setdefault("acc_tag", "")
            v.setdefault("buyer_confirmed", False)
        ORDERS = {k: RentOrder(**v) for k, v in d.items()}
    else:
        ORDERS = {}
    with _processed_lock:
        _processed_orders.update({oid: time.time() for oid in ORDERS.keys()})
    _cleanup_orders()
    d = _load_json("pwd_backups")
    PWD_BACKUPS = d if isinstance(d, dict) else {}

_load_all()

class AccountRepo:
    @staticmethod
    def get(acc_id: int) -> Optional[AccountModel]:
        return next((a for a in ACCOUNTS if a.id == acc_id), None)

    @staticmethod
    def by_order(order_id: str) -> Optional[AccountModel]:
        return next((a for a in ACCOUNTS if a.current_order == order_id), None)

    @staticmethod
    def get_free(tag: str) -> Optional[AccountModel]:
        tag = _ntag(tag)
        with _data_lock:
            candidates = sorted(
                [a for a in ACCOUNTS if _ntag(a.tag) == tag and a.status == RentStatus.FREE],
                key=lambda a: a.id
            )
            if not candidates:
                return None
            with _tag_queue_lock:
                idx = _tag_queue_index.get(tag, 0) % len(candidates)
            return candidates[idx]

    @staticmethod
    def count_free(tag: str = None) -> Dict[str, int]:
        result = {}
        with _data_lock:
            snapshot = list(ACCOUNTS)
        for a in snapshot:
            if a.status != RentStatus.FREE:
                continue
            t = _ntag(a.tag)
            if tag is not None and t != _ntag(tag):
                continue
            result[t] = result.get(t, 0) + 1
        return result

    @staticmethod
    def claim_free(tag, order_id, buyer, buyer_id, chat_id, hours: float) -> Optional[AccountModel]:
        tag_n = _ntag(tag)
        with _data_lock:
            candidates = sorted(
                [a for a in ACCOUNTS if _ntag(a.tag) == tag_n and a.status == RentStatus.FREE],
                key=lambda a: a.id
            )
            if not candidates:
                return None
            with _tag_queue_lock:
                # Индекс хранится как позиция среди свободных кандидатов (пересчитывается каждый раз)
                # Это предотвращает сдвиг после того как кандидаты выбывают из списка
                raw_idx = _tag_queue_index.get(tag_n, 0)
                idx = raw_idx % len(candidates)
                # Следующий индекс — просто +1, обёрнутый по актуальному размеру
                _tag_queue_index[tag_n] = (idx + 1) % len(candidates)
            chosen = candidates[idx]
            chosen.status = RentStatus.ACTIVE
            chosen.current_order = order_id
            chosen.owner = buyer
            chosen.owner_id = buyer_id
            chosen.owner_chat_id = chat_id
            chosen.rental_start = _fmt(_now())
            chosen.rental_end = _fmt(_now() + timedelta(hours=hours))
            _save_accounts()
            return chosen

    @staticmethod
    def add(login, password, mafile, tag) -> Tuple[bool, str]:
        tag = _ntag(tag)
        with _data_lock:
            if any(a.login.lower() == login.lower() for a in ACCOUNTS):
                return False, "Аккаунт уже существует"
            nid = max((a.id for a in ACCOUNTS), default=0) + 1
            ACCOUNTS.append(AccountModel(
                id=nid, login=login, password=password, mafile=mafile, tag=tag))
            _save_accounts()
        return True, f"Аккаунт {login} добавлен (ID: {nid}, тег: {tag})"

    @staticmethod
    def delete(acc_id: int) -> bool:
        with _data_lock:
            for i, a in enumerate(ACCOUNTS):
                if a.id == acc_id:
                    del ACCOUNTS[i]
                    _save_accounts()
                    with _acc_pwd_locks_mutex:
                        _acc_pwd_locks.pop(acc_id, None)
                    return True
        return False

    @staticmethod
    def assign(acc_id, order_id, buyer, buyer_id, chat_id, hours: float):
        with _data_lock:
            acc = AccountRepo.get(acc_id)
            if not acc:
                return
            acc.status = RentStatus.ACTIVE
            acc.current_order = order_id
            acc.owner = buyer
            acc.owner_id = buyer_id
            acc.owner_chat_id = chat_id
            acc.rental_start = _fmt(_now())
            acc.rental_end = _fmt(_now() + timedelta(hours=hours))
            _save_accounts()

    @staticmethod
    def extend_rent(acc_id: int, hours: float) -> Optional[str]:
        with _data_lock:
            acc = AccountRepo.get(acc_id)
            if acc and acc.rental_end:
                acc.rental_end = _fmt(_parse(acc.rental_end) + timedelta(hours=hours))
                _save_accounts()
                return acc.rental_end
        return None

    @staticmethod
    def release(acc_id: int, new_password: str = None, error: bool = False):
        with _data_lock:
            acc = AccountRepo.get(acc_id)
            if not acc:
                return
            acc.status = RentStatus.ERROR if error else RentStatus.FREE
            acc.current_order = acc.owner = acc.owner_id = None
            acc.owner_chat_id = acc.rental_start = acc.rental_end = None
            acc.access_count = 0
            if new_password:
                acc.password = new_password
                _record_password_backup(acc.id, acc.login, new_password, "bot")
            _save_accounts()
            if not error and SETTINGS and SETTINGS.auto_enable_lots and cardinal_ref:
                acc_tag_local = _ntag(acc.tag)
                free_after = len([a for a in ACCOUNTS
                                   if _ntag(a.tag) == acc_tag_local and a.status == RentStatus.FREE])
                if free_after == 1:
                    def _auto_enable_release(tag=acc_tag_local):
                        toggled = _toggle_fp_lots_for_tag(cardinal_ref, tag, True)
                        if toggled and tg_logs:
                            tg_logs.lots_auto_enabled(tag, toggled)
                    threading.Thread(target=_auto_enable_release, daemon=True).start()
            if error and SETTINGS and SETTINGS.auto_free_on_error:
                AccountRepo.reset_to_free(acc_id)

    @staticmethod
    def reset_to_free(acc_id: int):
        with _data_lock:
            acc = AccountRepo.get(acc_id)
            if not acc:
                return
            acc.status = RentStatus.FREE
            acc.current_order = acc.owner = acc.owner_id = None
            acc.owner_chat_id = acc.rental_start = acc.rental_end = None
            acc.access_count = 0
            _save_accounts()

    @staticmethod
    def manual_assign(acc_id: int, buyer: str, hours: float) -> Optional[AccountModel]:
        with _data_lock:
            acc = AccountRepo.get(acc_id)
            if not acc or acc.status not in (RentStatus.FREE, RentStatus.ERROR):
                return None
            oid = f"manual_{acc_id}_{int(time.time())}"
            now = _now()
            acc.status = RentStatus.ACTIVE
            acc.current_order = oid
            acc.owner = buyer
            acc.owner_id = acc.owner_chat_id = None
            acc.rental_start = _fmt(now)
            acc.rental_end = _fmt(now + timedelta(hours=hours))
            acc.access_count = 0
            ORDERS[oid] = RentOrder(id=oid, chat_id=None, buyer=buyer, buyer_id=0,
                                    acc_id=acc.id, acc_login=acc.login, acc_tag=_ntag(acc.tag),
                                    hours=hours, status=RentStatus.ACTIVE)
            _save_accounts()
            _save_orders()
            return acc

    @staticmethod
    def set_password(acc_id: int, new_password: str) -> bool:
        with _data_lock:
            acc = AccountRepo.get(acc_id)
            if not acc:
                return False
            acc.password = new_password
            _save_accounts()
            _record_password_backup(acc.id, acc.login, new_password, "human")
            return True

    @staticmethod
    def set_password_bot(acc_id: int, new_password: str) -> bool:
        """Обновляет пароль после смены, выполненной ботом (например, по кнопке «сменить пароль»)."""
        with _data_lock:
            acc = AccountRepo.get(acc_id)
            if not acc:
                return False
            acc.password = new_password
            _save_accounts()
            _record_password_backup(acc.id, acc.login, new_password, "bot")
            return True

    @staticmethod
    def set_mafile(acc_id: int, mafile: Dict[str, Any]) -> Tuple[bool, str]:
        with _data_lock:
            acc = AccountRepo.get(acc_id)
            if not acc:
                return False, "Аккаунт не найден"
            missing = _validate_mafile(mafile)
            if missing:
                return False, f"Отсутствует в maFile: {', '.join(missing)}"
            acc.mafile = mafile
            new_login = mafile.get("account_name")
            if isinstance(new_login, str) and new_login.strip():
                acc.login = new_login.strip()
            _save_accounts()
            return True, ""

    @staticmethod
    def get_stats() -> dict:
        r = {s: 0 for s in (RentStatus.FREE, RentStatus.ACTIVE, RentStatus.ERROR)}
        for a in ACCOUNTS:
            if a.status in r:
                r[a.status] += 1
        r["total"] = len(ACCOUNTS)
        return r

    @staticmethod
    def all_tags() -> List[str]:
        return list({_ntag(a.tag) for a in ACCOUNTS})

    @staticmethod
    def find_active_by_buyer(buyer_id: int, tag: str = None) -> Optional[RentOrder]:
        for o in ORDERS.values():
            if o.status != RentStatus.ACTIVE:
                continue
            if o.buyer_id == buyer_id:
                if tag is None:
                    return o
                acc = AccountRepo.get(o.acc_id)
                if acc and _ntag(acc.tag) == _ntag(tag):
                    return o
        return None

    @staticmethod
    def find_active_by_name(buyer_name: str) -> Optional[RentOrder]:
        """
        Ищет активную аренду по нику покупателя.
        Сначала смотрит напрямую в аккаунтах (acc.owner), потом в ORDERS (o.buyer).
        Это основной фикс бага '❌ Активный заказ не найден' при !код / !time.
        """
        if not buyer_name:
            return None
        bl = buyer_name.strip().lower()
        # Шаг 1: прямой поиск в аккаунтах по полю owner — самый надёжный
        for acc in ACCOUNTS:
            if acc.status == RentStatus.ACTIVE and acc.owner:
                if acc.owner.strip().lower() == bl:
                    if acc.current_order and acc.current_order in ORDERS:
                        o = ORDERS[acc.current_order]
                        if o.status == RentStatus.ACTIVE:
                            logger.debug(
                                f"[ASRplus] find_active_by_name '{bl}': "
                                f"найден через acc.owner -> order #{o.id}"
                            )
                            return o
        # Шаг 2: поиск в ORDERS по полю buyer
        for o in ORDERS.values():
            if o.status != RentStatus.ACTIVE:
                continue
            if o.buyer and o.buyer.strip().lower() == bl:
                logger.debug(
                    f"[ASRplus] find_active_by_name '{bl}': "
                    f"найден через o.buyer -> order #{o.id}"
                )
                return o
        return None

    @staticmethod
    def find_order_by_chat(chat_id, author_id=None, author_name=None) -> Optional[RentOrder]:
        """
        Ищет активный заказ для данного чата/пользователя.
        Порядок приоритетов:
          1. Прямой поиск по нику в аккаунтах (acc.owner) — фикс основного бага
          2. chat_id совпадение в ORDERS
          3. buyer_id совпадение в ORDERS
          4. buyer (ник) совпадение в ORDERS
          5. owner_id совпадение в аккаунтах
          6. owner_chat_id совпадение в аккаунтах
        """
        logger.debug(
            f"[ASRplus] find_order_by_chat: chat_id={chat_id}, "
            f"author_id={author_id}, author_name={author_name}, "
            f"active_orders={[o.id for o in ORDERS.values() if o.status == RentStatus.ACTIVE]}"
        )
        key = str(chat_id)

        # ── Шаг 1: поиск по нику напрямую в аккаунтах (самый точный) ──────
        if author_name:
            found = AccountRepo.find_active_by_name(author_name)
            if found:
                return found

        # ── Шаг 2: chat_id совпадение в ORDERS ───────────────────────────
        for o in ORDERS.values():
            if o.status in (RentStatus.FINISHED, RentStatus.REFUND):
                continue
            if str(o.chat_id or "") == key:
                return o

        # ── Шаг 3: buyer_id совпадение в ORDERS ──────────────────────────
        if author_id and author_id > 0:
            for o in ORDERS.values():
                if o.status in (RentStatus.FINISHED, RentStatus.REFUND):
                    continue
                if o.buyer_id == author_id:
                    return o

        # ── Шаг 4: ник в ORDERS (дубль на случай расхождения с acc.owner) ─
        if author_name:
            al = author_name.strip().lower()
            for o in ORDERS.values():
                if o.status in (RentStatus.FINISHED, RentStatus.REFUND):
                    continue
                if o.buyer and o.buyer.strip().lower() == al:
                    return o

        # ── Шаг 5: owner_id в аккаунтах ──────────────────────────────────
        if author_id and author_id > 0:
            for acc in ACCOUNTS:
                if acc.status == RentStatus.ACTIVE and acc.owner_id == author_id:
                    if acc.current_order and acc.current_order in ORDERS:
                        return ORDERS[acc.current_order]

        # ── Шаг 6: owner_chat_id в аккаунтах ─────────────────────────────
        for acc in ACCOUNTS:
            if acc.status == RentStatus.ACTIVE and acc.owner_chat_id:
                if str(acc.owner_chat_id) == key:
                    if acc.current_order and acc.current_order in ORDERS:
                        return ORDERS[acc.current_order]

        return None

    @staticmethod
    def find_tag_by_chat(chat_id, author_id=None, author_name=None) -> Optional[str]:
        order = AccountRepo.find_order_by_chat(chat_id, author_id, author_name)
        if order:
            acc = AccountRepo.get(order.acc_id)
            if acc:
                return _ntag(acc.tag)
        return None

class TgLogs:
    def __init__(self, c: Cardinal):
        self.c = c
        self.bot = c.telegram.bot

    def _send(self, text):
        for uid in self.c.telegram.authorized_users:
            try:
                self.bot.send_message(uid, f"⚡ <b>ASR+ v{VERSION}</b>\n{text}", parse_mode="HTML")
            except Exception:
                pass

    def order_completed(self, order, login):
        if SETTINGS.notification_order_completed:
            acc = AccountRepo.get(order.acc_id)
            end_time = (acc.rental_end if acc else None) or "—"
            self._send(
                f"✅ Новый заказ выдан\n"
                f"∟ Заказ: #{order.id[:12]}...\n"
                f"∟ Покупатель: <b>{order.buyer}</b>\n"
                f"∟ Аккаунт: <code>{login}</code>\n"
                f"∟ Часов: <code>{int(order.hours)}</code>\n"
                f"∟ Аренда до: <code>{end_time}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>© ASR+ by @DzhantDev</i>"
            )

    def error(self, msg):
        if SETTINGS.notification_error:
            self._send(f"❌ Ошибка: {msg}")

    def refund(self, order_id, reason):
        if SETTINGS.notification_refund:
            self._send(f"💰 Возврат #{order_id[:12]}...\n∟ Причина: {reason}")

    def lots_auto_disabled(self, tag: str, lot_ids: List[str]):
        self._send(f"🔴 Авто-выключение лотов\n∟ Тег: <code>{tag}</code>\n∟ Лоты: {', '.join(f'#{lid}' for lid in lot_ids)}")

    def lots_auto_enabled(self, tag: str, lot_ids: List[str]):
        self._send(f"🟢 Авто-включение лотов\n∟ Тег: <code>{tag}</code>\n∟ Лоты: {', '.join(f'#{lid}' for lid in lot_ids)}")

    def seller_called(self, chat_id, buyer: str, order_id: str = ""):
        chat_url = FUNPAY_CHAT_URL.format(chat_id)
        order_line = f"∟ Заказ: #{order_id[:12]}...\n" if order_id else ""
        self._send(
            f"📞 <b>Покупатель зовёт продавца!</b>\n"
            f"∟ Покупатель: <b>{buyer}</b>\n"
            f"{order_line}"
            f"∟ Чат: <a href=\"{chat_url}\">открыть чат</a>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>© ASR+ by @DzhantDev</i>"
        )

def _tmpl(template: str, **kw) -> str:
    r = template
    # Если есть hours — добавляем $hours_word для правильного склонения
    if "hours" in kw:
        try:
            kw.setdefault("hours_word", _hours_word(float(kw["hours"])))
        except Exception:
            kw.setdefault("hours_word", "часов")
    # Сортируем по убыванию длины ключа, чтобы $hours_word заменялся раньше $hours
    for k, v in sorted(kw.items(), key=lambda x: len(x[0]), reverse=True):
        r = r.replace(f"${k}", str(v))
    return r

def _send_fp(c, chat_id, text):
    try:
        c.send_message(chat_id, text)
    except Exception as e:
        logger.warning(f"[ASRplus] send_message: {e}")

def _do_refund(c, order_id) -> bool:
    if not (SETTINGS and SETTINGS.autoback_on_error):
        logger.debug(f"[ASRplus] _do_refund #{order_id}: авто-возврат выключен, пропуск")
        return False
    try:
        c.account.refund(order_id)
        return True
    except Exception:
        return False

def _extract_lot_id_from_html(html: str) -> Optional[str]:
    if not html:
        return None
    for pat in (r'/lots/[^"\']*offer[^"\']*[?&]id=(\d+)',
                r'href=["\'][^"\']*[?&]id=(\d+)',
                r'data-offer=["\'](\d+)',
                r'data-id=["\'](\d+)'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None

def _get_order_quantity(c, order_id: str, event_order=None) -> int:
    if event_order is not None:
        for attr in ("quantity", "amount", "count"):
            val = getattr(event_order, attr, None)
            if val:
                try:
                    qty = int(val)
                    if qty > 0:
                        return qty
                except (ValueError, TypeError):
                    pass
        html = getattr(event_order, 'html', None) or getattr(event_order, 'description', None)
        if html:
            m = re.search(r'Количество[:\s]+(\d+)', html)
            if m:
                return int(m.group(1))
    try:
        order = c.account.get_order(order_id)
        if hasattr(order, 'quantity') and order.quantity:
            qty = int(order.quantity)
            if qty > 0:
                return qty
        if hasattr(order, 'html') and order.html:
            m = re.search(r'Количество[:\s]+(\d+)', order.html)
            if m:
                return int(m.group(1))
    except Exception as e:
        logger.warning(f"[ASRplus] Не удалось получить quantity заказа {order_id}: {e}")
    logger.warning(f"[ASRplus] quantity не найден для заказа {order_id}, используем 1")
    return 1

_TAG_RE = re.compile(r'#([a-zA-Zа-яА-ЯёЁ0-9_\-]{2,32})')

def _parse_hash_tag_from_text(text: str, exclude_id: Optional[str] = None) -> Optional[str]:
    """
    Ищет первый #тег в тексте, который совпадает с тегом
    существующего аккаунта в базе.
    exclude_id — если указан (обычно ID заказа), кандидаты, совпадающие с ним
    (например "#41234567" — это подставленный FunPay номер заказа, а не тег),
    игнорируются.
    Возвращает нормализованный тег или None.
    """
    if not text:
        return None
    excl = _ntag(str(exclude_id)) if exclude_id else None
    matches = _TAG_RE.findall(text)
    for match in matches:
        candidate = _ntag(match)
        if excl and candidate == excl:
            continue
        # Игнорируем чисто цифровые теги — это почти наверняка номер заказа
        # (#41234567), а не пользовательский тег вида #repo/#cs2.
        if candidate.isdigit():
            continue
        # Приоритет 1: совпадение с тегом аккаунта
        if any(_ntag(a.tag) == candidate for a in ACCOUNTS):
            return candidate
        # Приоритет 2: совпадение с тегом лота в настройках
        for lid in SETTINGS.lots:
            cfg = SETTINGS.get_lot(lid)
            if cfg and _ntag(cfg.tag) == candidate:
                return candidate
    return None

def _get_lot_detailed_description(c, lot_id: str):
    """
    Получает объект полей лота и текст ПОДРОБНОГО ОПИСАНИЯ через FunPayAPI
    (используется и для чтения #тега, и для его автоматической записи
    кнопкой "Авто-тег").

    ВАЖНО (источник бага в 1.1.0): при сохранении лота Account.save_lot()
    вызывает lot_fields.renew_fields(), который ПЕРЕСОБИРАЕТ сырой словарь
    lf.fields из атрибутов объекта (description_ru / description_en), а НЕ
    наоборот. Поэтому правка lf.fields["fields[desc][ru]"] напрямую молча
    терялась при сохранении — FunPayAPI откатывал её обратно на оригинальный
    текст. Читать и писать подробное описание нужно строго через атрибуты
    lf.description_ru / lf.description_en, а не через lf.fields.

    Возвращает (lot_fields, lang, desc_text), где lang — 'ru' или 'en'
    (какое из полей фактически используется), либо (None, None, "") при ошибке.
    """
    try:
        lf = c.account.get_lot_fields(int(lot_id))
    except Exception as e:
        logger.debug(f"[ASRplus] get_lot_fields({lot_id}): {e}")
        return None, None, ""
    if not lf:
        return None, None, ""
    desc_ru = getattr(lf, "description_ru", None) or ""
    desc_en = getattr(lf, "description_en", None) or ""
    if desc_ru:
        return lf, "ru", desc_ru
    if desc_en:
        return lf, "en", desc_en
    # Оба поля пустые (новый лот без описания) — по умолчанию работаем с ru
    return lf, "ru", ""

def _extract_tag_from_lot_description(c, lot_id: str) -> Optional[str]:
    """
    Ищет #тег в ПОДРОБНОМ ОПИСАНИИ лота на FunPay по lot_id.
    Продавец сам пишет #тег в конец подробного описания лота — это
    приоритетный источник тега.
    Возвращает нормализованный тег или None.
    """
    if not lot_id:
        return None
    try:
        _, _, desc_text = _get_lot_detailed_description(c, lot_id)
        if desc_text:
            tag = _parse_hash_tag_from_text(desc_text)
            if tag:
                logger.info(
                    f"[ASRplus] #тег найден в подробном описании лота #{lot_id}: #{tag}"
                )
                return tag
    except Exception as e:
        logger.debug(f"[ASRplus] _extract_tag_from_lot_description({lot_id}): {e}")
    return None

def _write_tag_to_funpay_lot(c, funpay_lot_id: str, tag: str) -> Tuple[bool, str]:
    """
    Дописывает '#tag' в конец подробного описания лота funpay_lot_id на FunPay,
    если его там ещё нет. Работает с ЛЮБЫМ ID лота на FunPay напрямую через API —
    не требует, чтобы лот был зарегистрирован в SETTINGS.lots. Используется как
    для основных лотов (см. _auto_write_match_tag), так и для лотов-продления,
    которым тег нужен для того, чтобы бот мог распознать, к какому основному
    лоту относится оплата, пришедшая именно с лота-продления.

    Возвращает (ok, message).
    """
    try:
        lf, lang, desc_text = _get_lot_detailed_description(c, funpay_lot_id)
    except Exception as e:
        return False, f"Не удалось получить описание лота: {_safe_err(e)}"
    if lf is None:
        return False, "Не удалось получить подробное описание лота"
    if _ntag(tag) in {_ntag(m) for m in _TAG_RE.findall(desc_text or "")}:
        return True, "ID уже есть в описании"
    new_desc = f"{desc_text.rstrip()}\n#{tag}" if desc_text and desc_text.strip() else f"#{tag}"
    try:
        # ВАЖНО: пишем через атрибут description_ru/description_en, а не
        # через lf.fields напрямую — иначе save_lot() откатит изменение
        # (см. комментарий в _get_lot_detailed_description).
        if lang == "en":
            lf.description_en = new_desc
        else:
            lf.description_ru = new_desc
        c.account.save_lot(lf)
        _invalidate_lots_cache()
        return True, f"ID #{tag} добавлен в описание лота"
    except Exception as e:
        return False, f"Ошибка записи в описание: {_safe_err(e)}"

def _auto_write_match_tag(c, lot_id: str) -> Tuple[bool, Optional[str], str]:
    """
    Гарантирует, что у лота lot_id есть уникальный служебный ID (система как
    и дописывает его в конец подробного описания лота на FunPay
    через API, если его там ещё нет.

    Используется как при добавлении нового лота (вызывается автоматически,
    чтобы продавцу не нужно было ничего нажимать руками), так и вручную
    кнопкой «🏷 Авто ID» — для лотов, привязанных ещё до появления этой
    системы.

    Возвращает (ok, tag, message):
      ok — True, если ID гарантированно есть в описании лота (был там уже
           или был успешно дописан) либо False при ошибке;
      tag — сам ID (без #), либо None если создать не удалось;
      message — короткое человекочитаемое пояснение результата.
    """
    lc = SETTINGS.get_lot(lot_id)
    if not lc:
        return False, None, "Лот не найден"
    target_tag = SETTINGS.ensure_match_tag(lot_id)
    if not target_tag:
        return False, None, "Не удалось создать ID для лота"
    ok, msg = _write_tag_to_funpay_lot(c, lot_id, target_tag)
    return ok, target_tag, msg

def _match_lot_by_match_tag(description: str, order_id: Optional[str] = None) -> Optional[str]:
    """
    Основной и самый надёжный способ определить лот заказа — по уникальному
    служебному ID лота (см. LotConfig.match_tag).

    Правило: в тексте заказа/лота может встречаться несколько #тегов подряд
    (например скопированный номер заказа "#41234567", теги игры/категории
    и т.д.). Мы идём по всем найденным #тегам ПО ПОРЯДКУ появления в тексте:
      - тег, совпадающий с ID самого заказа — это подставленный FunPay
        номер заказа, а не тег лота, поэтому он всегда игнорируется молча
        и просмотр продолжается дальше;
      - как только среди оставшихся тегов встречается ID, сохранённый
        за каким-либо лотом (SETTINGS.lots[*].match_tag) — этот лот и
        считается искомым, поиск останавливается.
    """
    if not description or not SETTINGS.lots:
        return None

    # Гарантируем, что у всех настроенных лотов есть свой ID
    SETTINGS.ensure_all_match_tags()

    match_tag_map: Dict[str, str] = {}
    for lid in SETTINGS.lots:
        cfg = SETTINGS.get_lot(lid)
        if cfg and cfg.match_tag:
            match_tag_map[_ntag(cfg.match_tag)] = lid

    if not match_tag_map:
        return None

    excl = _ntag(str(order_id)) if order_id else None
    for raw in _TAG_RE.findall(description):
        candidate = _ntag(raw)
        if excl and candidate == excl:
            # Это номер заказа, подставленный в текст, а не тег лота — игнорируем и идём дальше
            continue
        lot_id = match_tag_map.get(candidate)
        if lot_id:
            logger.info(
                f"[ASRplus] _match_lot_by_match_tag: найден ID '{candidate}' -> лот {lot_id}"
            )
            return lot_id
    return None

def _match_lot_by_tag_keyword(description: str, order_id: Optional[str] = None) -> Optional[str]:
    """
    Шаг 5.5 (улучшенный): ищет тег для заказа несколькими способами.

    5.5.0: ищет уникальный ID лота (см. _match_lot_by_match_tag) —
           самый надёжный способ, не зависящий от совпадений с обычным тегом.

    5.5а: ищет #тег прямо в тексте описания заказа, совпадающий с обычным
          тегом лота/аккаунта. FunPay копирует подробное описание лота в
          описание заказа, поэтому если в лоте был указан #pz — он будет и
          в описании заказа. Если найден — сразу возвращает лот с тегом pz.

    5.5б: fallback — ищет тег как целое слово в описании заказа (word boundary),
          чтобы короткие теги типа 'gm' не срабатывали внутри других слов.
    """
    if not description or not SETTINGS.lots:
        return None

    # --- Шаг 5.5.0: ищем уникальный ID лота (приоритетный способ) ---
    match_tag_lot = _match_lot_by_match_tag(description, order_id)
    if match_tag_lot:
        return match_tag_lot

    desc_lower = description.strip().lower()

    # --- Шаг 5.5а: ищем #тег прямо в описании заказа ---
    hash_tag = _parse_hash_tag_from_text(description, exclude_id=order_id)
    if hash_tag:
        lot_id = SETTINGS.find_lot_id_by_tag(hash_tag)
        if lot_id:
            logger.info(
                f"[ASRplus] _match_lot_by_tag_keyword: найден #тег '{hash_tag}' "
                f"прямо в описании заказа -> лот {lot_id}"
            )
            return lot_id

    # --- Шаг 5.5б: fallback — точное совпадение тега как целого слова ---
    tag_to_lot: Dict[str, str] = {}
    for lid in SETTINGS.lots:
        cfg = SETTINGS.get_lot(lid)
        if cfg and cfg.tag:
            tag_to_lot[_ntag(cfg.tag)] = lid

    for acc in ACCOUNTS:
        t = _ntag(acc.tag)
        if t and t not in tag_to_lot:
            lot_id = SETTINGS.find_lot_id_by_tag(t)
            if lot_id:
                tag_to_lot[t] = lot_id

    best_lid: Optional[str] = None
    best_len = 0

    for tag, lid in tag_to_lot.items():
        if not tag or tag == "default":
            continue
        tag_lower = tag.lower()
        # Ищем тег только как целое слово, чтобы 'gm' не срабатывал внутри других слов
        pattern = r'(?<![a-zA-Zа-яА-ЯёЁ0-9_])' + re.escape(tag_lower) + r'(?![a-zA-Zа-яА-ЯёЁ0-9_])'
        if re.search(pattern, desc_lower):
            if len(tag_lower) > best_len:
                best_len = len(tag_lower)
                best_lid = lid

    if best_lid:
        logger.info(
            f"[ASRplus] _match_lot_by_tag_keyword: desc={description!r:.60} -> лот {best_lid}"
        )
    return best_lid

def _our_subcategory_ids() -> set:
    """Множество subcategory_id всех настроенных лотов (для фильтра 'наш раздел')."""
    ids = set()
    for lid in list(SETTINGS.lots.keys()):
        cfg = SETTINGS.get_lot(lid)
        sid = getattr(cfg, "subcategory_id", None) if cfg else None
        if sid is not None:
            try:
                ids.add(int(sid))
            except (TypeError, ValueError):
                pass
    return ids

def _order_subcategory_id(order) -> Optional[int]:
    sub = getattr(order, "subcategory", None)
    sid = getattr(sub, "id", None) if sub is not None else None
    try:
        return int(sid) if sid is not None else None
    except (TypeError, ValueError):
        return None

def _extract_raw_offer_id(c, event) -> Optional[str]:
    """Возвращает СЫРОЙ ID лота, с которого реально куплен заказ на FunPay —
    в отличие от _find_lot_id_for_order, здесь НЕ применяется резолвинг через
    тег/match_tag к основному лоту. Нужно, чтобы понять, с какого конкретно
    лота-продления (варианта с определённым временем) пришла оплата."""
    order = event.order
    order_id = getattr(order, "id", None)
    for attr in ("offer_id", "lot_id"):
        v = getattr(order, attr, None)
        if v is not None:
            return str(v)
    try:
        full = c.account.get_order(order_id) if order_id else None
    except Exception:
        full = None
    if full is not None:
        for attr in ("offer_id", "lot_id"):
            v = getattr(full, attr, None)
            if v is not None:
                return str(v)
        html = getattr(full, "html", None)
        if html:
            ex = _extract_lot_id_from_html(str(html))
            if ex:
                return ex
    html = getattr(order, "html", None)
    if html:
        ex = _extract_lot_id_from_html(str(html))
        if ex:
            return ex
    return None

def _find_lot_id_for_order(c, event) -> Optional[str]:
    """
    Определяет lot_id для заказа.
    После нахождения lot_id — проверяет подробное описание лота на #тег override.

    ВАЖНО: у события/виджета заказа (event.order, класс OrderShortcut) поле
    `.description` — это только КОРОТКОЕ описание (название) из списка сделок.
    ID, который дописывается в конец ПОДРОБНОГО описания лота, туда не
    попадает — он есть только в `full_description` объекта Order, который
    отдаётся отдельным полем через FunPayAPI и требует отдельного запроса
    `account.get_order(order_id)`. Поэтому весь поиск ID и тегов ведётся
    по объединённому тексту short_description + full_description, а не
    просто по `order.description`.
    """
    order = event.order
    html  = getattr(order, "html", None) or ""
    order_id = getattr(order, "id", None)

    def _found(lot_id: str) -> str:
        """Хук: вызывается когда lot_id найден. Возвращает lot_id (без изменений)."""
        return lot_id

    # Один запрос к FunPay API за полными данными заказа — используем его
    # и для поиска offer_id/lot_id, и (главное) для full_description, где
    # реально лежит ID, скопированный из подробного описания лота.
    full = None
    if order_id:
        try:
            full = c.account.get_order(order_id)
        except Exception as e:
            logger.debug(f"[ASRplus] get_order({order_id}) fallback: {e}")

    short_desc = getattr(order, "description", None) or getattr(full, "short_description", None) or ""
    full_desc  = getattr(full, "full_description", None) or ""
    combined_desc = f"{short_desc}\n{full_desc}".strip()

    # --- Шаг -1: структурная привязка лота-продления (САМЫЙ надёжный способ) ---
    # Проверяем raw offer_id/lot_id заказа напрямую против настроек плагина
    # (extend_lot_id / extend_options / tag_extend), ДО парсинга текстового
    # Auto ID из описания лота на FunPay. Текстовый Auto ID может устареть —
    # например, если физический лот FunPay был ранее привязан как продление
    # к другому основному лоту и тег в его описании не был перезаписан —
    # тогда шаг 0 привяжет заказ не туда. Структурные настройки бота такой
    # проблеме не подвержены и проверяются в первую очередь.
    for attr in ("offer_id", "lot_id"):
        v = getattr(order, attr, None)
        if v is not None:
            m_struct = SETTINGS.find_main_lot_by_configured_extend_id(str(v))
            if m_struct:
                logger.info(
                    f"[ASRplus] #{order_id}: лот найден шагом -1 (структурная "
                    f"привязка продления, offer={v}): {m_struct}"
                )
                return _found(m_struct)

    # --- Шаг 0: уникальный ID лота — самый надёжный способ, проверяем первым ---
    if combined_desc:
        m0 = _match_lot_by_match_tag(combined_desc, order_id)
        if m0:
            logger.info(f"[ASRplus] #{order_id}: лот найден шагом 0 (ID): {m0}")
            return _found(m0)

    
    for attr in ("offer_id", "lot_id"):
        v = getattr(order, attr, None)
        if v is not None:
            sv = str(v)
            if SETTINGS.has_lot(sv):
                logger.info(f"[ASRplus] #{order_id}: лот найден шагом 1 ({attr}={sv})")
                return _found(sv)

    
    extracted = _extract_lot_id_from_html(html)
    if extracted and SETTINGS.has_lot(extracted):
        logger.info(f"[ASRplus] #{order_id}: лот найден шагом 2 (HTML): {extracted}")
        return _found(extracted)

    
    if full is not None:
        for attr in ("offer_id", "lot_id"):
            v = getattr(full, attr, None)
            if v is not None and SETTINGS.has_lot(str(v)):
                logger.info(f"[ASRplus] #{order_id}: лот найден шагом 3 (API {attr}={v})")
                return _found(str(v))
        for attr in ("html",):
            v = getattr(full, attr, None)
            if v:
                ex = _extract_lot_id_from_html(str(v))
                if ex and SETTINGS.has_lot(ex):
                    logger.info(f"[ASRplus] #{order_id}: лот найден шагом 3 (API HTML)")
                    return _found(ex)
        if full_desc:
            ex = _extract_lot_id_from_html(full_desc)
            if ex and SETTINGS.has_lot(ex):
                logger.info(f"[ASRplus] #{order_id}: лот найден шагом 3 (API full_description)")
                return _found(ex)

    
    if combined_desc:
        m = _match_lot_by_description(c, combined_desc)
        if m:
            logger.info(f"[ASRplus] #{order_id}: лот найден шагом 4 (нечёткий): {m}")
            return _found(m)

    
    if combined_desc:
        m = _match_lot_by_tag_keyword(combined_desc, order_id)
        if m:
            logger.info(f"[ASRplus] #{order_id}: лот найден шагом 4.5 (тег по ключевому слову): {m}")
            return _found(m)


    logger.warning(f"[ASRplus] #{order_id}: лот не определён по шагам 1-4.5 — проверяем подкатегорию")
    return None

def _match_lot_by_description(c, description: str) -> Optional[str]:
    """Нечёткое сопоставление описания заказа с названиями лотов на FunPay."""
    if not description or not SETTINGS.lots:
        return None
    try:
        all_lots = _get_cached_lots(c)
    except Exception:
        return None
    our_lot_ids = set(SETTINGS.lots.keys())
    our_lots = [lot for lot in all_lots if str(lot.id) in our_lot_ids]
    if not our_lots:
        return None
    desc_clean = description.strip().lower()
    desc_parts = [p.strip() for p in desc_clean.split(',') if p.strip()]
    
    for lot in our_lots:
        lot_title = (getattr(lot, 'description', None) or getattr(lot, 'title', None) or '').strip().lower()
        if lot_title and desc_clean == lot_title:
            return str(lot.id)
    
    best_id, best_score = None, 0.0
    for lot in our_lots:
        lot_title = (getattr(lot, 'description', None) or getattr(lot, 'title', None) or '').strip().lower()
        if not lot_title:
            continue
        lot_parts = [p.strip() for p in lot_title.split(',') if p.strip()]
        if not lot_parts or not desc_parts:
            continue
        matching = sum(1 for dp in desc_parts if dp in lot_parts)
        if matching > 0:
            score = matching / max(len(desc_parts), len(lot_parts))
            if score > best_score:
                best_score = score
                best_id = str(lot.id)
    return best_id if best_score >= 0.8 else None

def _build_stock_message(tag: str = None) -> str:
    free_counts = AccountRepo.count_free(tag)
    if not free_counts:
        return SETTINGS.messages.stock_empty
    lines = [f"∟ {t}: {cnt} шт." for t, cnt in sorted(free_counts.items())]
    return _tmpl(SETTINGS.messages.stock_info, stock_list="\n".join(lines))

PASSWORD_CHANGE_MAX_RETRIES = 2       # доп. попытки при явно временных (сетевых) ошибках
PASSWORD_CHANGE_RETRY_DELAY = 8       # секунд между повторными попытками

def _change_password_with_retry(acc) -> str:
    """Меняет пароль аккаунта с автоматическим повтором при временных ошибках
    (сеть/таймаут/капча Steam). Если ошибка окончательная (неверный пароль,
    битый maFile, требуется email-подтверждение и т.п.) — повторов не будет,
    исключение пробрасывается сразу, чтобы не тратить время впустую."""
    attempt = 0
    last_exc = None
    while attempt <= PASSWORD_CHANGE_MAX_RETRIES:
        try:
            return change_password_sync(acc.mafile, acc.password, acc.id)
        except Exception as e:
            last_exc = e
            code, desc, is_transient = _classify_error(e)
            if not is_transient or attempt == PASSWORD_CHANGE_MAX_RETRIES:
                raise
            logger.warning(
                f"[ASRplus] Смена пароля {acc.login}: временная ошибка [{code}] {desc} — "
                f"повтор {attempt + 1}/{PASSWORD_CHANGE_MAX_RETRIES} через {PASSWORD_CHANGE_RETRY_DELAY}с"
            )
            time.sleep(PASSWORD_CHANGE_RETRY_DELAY)
            attempt += 1
    raise last_exc

def _get_buyer_active_targets(buyer_id: int) -> List[Tuple[Any, Any]]:
    """Возвращает список (order, account) по всем активным арендам покупателя,
    без дублей по аккаунту. Общая функция для !код, !продлить и предупреждения
    об окончании аренды — раньше эта логика была продублирована в разных местах,
    что повышало риск рассинхронизации при правках."""
    if not buyer_id:
        return []
    active = [o for o in ORDERS.values() if o.status == RentStatus.ACTIVE and o.buyer_id == buyer_id]
    seen: Set[int] = set()
    result = []
    for o in sorted(active, key=lambda x: x.created_at):
        if o.acc_id in seen:
            continue
        acc = AccountRepo.get(o.acc_id)
        if acc and acc.status == RentStatus.ACTIVE and acc.current_order == o.id:
            result.append((o, acc))
            seen.add(o.acc_id)
    return result

def _effective_extend_lot_id(lot_cfg: "LotConfig") -> Optional[str]:
    """Лот-продление (одиночный) для лота: если задан индивидуально — берём его,
    иначе — общий для тега этого лота (если настроен)."""
    if not lot_cfg:
        return None
    if lot_cfg.extend_lot_id:
        return lot_cfg.extend_lot_id
    return SETTINGS.get_tag_extend_lot_id(lot_cfg.tag)

def _effective_extend_options(lot_cfg: "LotConfig") -> List[Dict[str, Any]]:
    """Варианты продления (время) для лота: индивидуальные (если заданы),
    иначе общие для тега этого лота."""
    if not lot_cfg:
        return []
    if lot_cfg.extend_options:
        return lot_cfg.extend_options
    return SETTINGS.get_tag_extend_options(lot_cfg.tag)

def _enable_extend_lot_target(c, order, extend_lot_id: str, hard_timer: bool = True) -> Optional[str]:
    """Включает конкретный лот-продление (по его ID) для заказа и возвращает ссылку на него."""
    if not extend_lot_id:
        return None
    threading.Thread(
        target=lambda: _toggle_single_lot(c, extend_lot_id, True),
        daemon=True
    ).start()
    if hard_timer:
        _schedule_extend_lot_disable(c, extend_lot_id, order.id)
    return FUNPAY_LOT_URL.format(lot_id=extend_lot_id)

def _enable_extend_lot(c, order, hard_timer: bool = True) -> Optional[str]:
    """Включает лот-продление для заказа (если настроен, старая схема — один
    лот-продление на весь лот, либо общий для тега) и возвращает ссылку на него."""
    lot_cfg = SETTINGS.get_lot(order.lot_id) if order.lot_id else None
    extend_lot_id = _effective_extend_lot_id(lot_cfg)
    return _enable_extend_lot_target(c, order, extend_lot_id, hard_timer=hard_timer)

# Ожидание выбора времени продления покупателем (для лотов типа "fixed" с
# несколькими вариантами лотов-продлений): chat_id -> {"order_id", "options", "expire"}
_extend_choice_pending: Dict[int, Dict[str, Any]] = {}
_extend_choice_lock = threading.Lock()
EXTEND_CHOICE_TIMEOUT = 300  # 5 минут на выбор варианта продления

def _fmt_hours(h) -> str:
    try:
        h = float(h)
        return (f"{h:.0f}" if h == int(h) else f"{h:g}")
    except Exception:
        return str(h)

def _notify_rent_ending_soon(c, order):
    """Шлёт предупреждение об окончании аренды. Если у покупателя всего один
    активный аккаунт — обычное сообщение с призывом написать !продлить.
    Если 2 и более — присылает список 'Логин - ссылка на продление' по каждому,
    чтобы не было путаницы, какой именно аккаунт продлевать."""
    if not order.chat_id:
        order.update(warned=True)
        return
    targets = _get_buyer_active_targets(order.buyer_id)
    if len(targets) < 2:
        _send_fp(c, order.chat_id, SETTINGS.messages.warning)
        order.update(warned=True)
        return
    lines = []
    for o, acc in targets:
        link = _enable_extend_lot(c, o)
        if link:
            lines.append(f"{acc.login} - {link}")
        else:
            lines.append(f"{acc.login} - продление недоступно, обратитесь к продавцу")
        o.update(warned=True)
    _send_fp(c, order.chat_id, _tmpl(SETTINGS.messages.warning_multi, accounts_list="\n".join(lines)))

def _recover_account(c, acc, order, reason):
    acc_tag = _ntag(acc.tag)
    was_last_free = SETTINGS.auto_enable_lots and cardinal_ref and \
        AccountRepo.count_free(acc_tag).get(acc_tag, 0) == 0
    try:
        np = _change_password_with_retry(acc)
        AccountRepo.release(acc.id, np)
        if was_last_free:
            def _auto_enable_recover(tag=acc_tag):
                toggled = _toggle_fp_lots_for_tag(cardinal_ref, tag, True)
                if toggled and tg_logs:
                    tg_logs.lots_auto_enabled(tag, toggled)
            threading.Thread(target=_auto_enable_recover, daemon=True).start()
        if order:
            order.update(status=RentStatus.FINISHED)
            if reason == "TIME" and order.chat_id:
                _send_fp(c, order.chat_id, _tmpl(SETTINGS.messages.rent_over, id=order.id))
    except SteamEmailVerificationRequired as e:
        code, desc, _ = _classify_error(e)
        logger.error(f"[ASRplus] Смена пароля {acc.login}: [{code}] {desc}")
        AccountRepo.release(acc.id, error=True)
        if tg_logs:
            tg_logs.error(f"⚠️ <b>{acc.login}</b>\n∟ Причина: {desc}")
        if order:
            if _do_refund(c, order.id):
                order.update(status=RentStatus.REFUND)
                if tg_logs:
                    tg_logs.refund(order.id, f"[{code}] {desc}: {acc.login}")
        return
    except Exception as e:
        code, desc, _ = _classify_error(e)
        logger.error(f"[ASRplus] Смена пароля не удалась: {acc.login} — [{code}] {desc} (raw: {_safe_err(e)})")
        AccountRepo.release(acc.id, error=True)
        if tg_logs:
            tg_logs.error(f"🔑 Не удалось сменить пароль: <b>{acc.login}</b>\n∟ Причина: {desc}\n∟ Код: <code>{code}</code>")
        if order:
            if _do_refund(c, order.id):
                order.update(status=RentStatus.REFUND)
                if tg_logs:
                    tg_logs.refund(order.id, f"[{code}] {desc}: {acc.login}")

def _stats_text() -> str:
    now = time.time()
    finished_all = [o for o in ORDERS.values() if o.status == RentStatus.FINISHED]
    refunds_all = sum(1 for o in ORDERS.values() if o.status == RentStatus.REFUND)
    exts_all = sum(1 for o in ORDERS.values() if o.is_extension)
    h_all = sum(o.hours for o in finished_all)
    def agg(ts):
        threshold = _fmt(MOSCOW_TZ.localize(datetime.fromtimestamp(ts)))
        arr = [o for o in finished_all if o.created_at >= threshold]
        return len(arr), sum(o.hours for o in arr)
    c_d, h_d = agg(now - 86400)
    c_w, h_w = agg(now - 604800)
    c_m, h_m = agg(now - 2592000)
    s = AccountRepo.get_stats()
    return (f"📊 <b>Статистика</b>\n\n"
            f"Аккаунтов: {s['total']} | 🟢{s[RentStatus.FREE]} 👤{s[RentStatus.ACTIVE]} "
            f"❌{s[RentStatus.ERROR]}\n\n"
            f"∟ Сегодня: <code>{c_d}</code> аренд | <code>{h_d:.0f}</code> ч\n"
            f"∟ Неделя: <code>{c_w}</code> аренд | <code>{h_w:.0f}</code> ч\n"
            f"∟ Месяц: <code>{c_m}</code> аренд | <code>{h_m:.0f}</code> ч\n"
            f"∟ Всего: <code>{len(finished_all)}</code> аренд | <code>{h_all:.0f}</code> ч\n\n"
            f"Возвратов: {refunds_all} | Продлений: {exts_all}")

def _order_detail_text(order_id: str):
    o = ORDERS.get(order_id)
    if not o:
        return "❌ Заказ не найден", None
    status_map = {
        RentStatus.FINISHED: "✅ Завершён", RentStatus.REFUND: "💰 Возврат",
        RentStatus.ACTIVE: "👤 Активна", RentStatus.ERROR: "❌ Ошибка"
    }
    st = status_map.get(o.status, o.status)
    acc = AccountRepo.get(o.acc_id)
    acc_name = acc.login if acc else (o.acc_login or f"#{o.acc_id}")
    order_url = FUNPAY_ORDER_URL.format(o.id)
    txt = f"📋 <b>Заказ <a href='{order_url}'>#{o.id}</a></b>\n\n"
    txt += f"∟ Статус: <b>{st}</b>\n"
    txt += f"∟ Покупатель: <code>{o.buyer}</code>\n"
    txt += f"∟ Аккаунт: <code>{acc_name}</code>\n"
    if o.lot_id:
        txt += f"∟ Лот: <code>{o.lot_id}</code>\n"
    txt += f"∟ Тег: <code>{o.acc_tag or '—'}</code>\n"
    txt += f"∟ Часов: <code>{o.hours}</code>\n"
    txt += f"∟ Создан: <code>{o.created_at[:19]}</code>\n"
    if o.is_extension:
        txt += "∟ Тип: 🔄 Продление\n"
    if acc and acc.rental_end and o.status == RentStatus.ACTIVE:
        txt += f"∟ Осталось: <code>{_remaining_str(acc.rental_end)}</code>\n"
    if o.chat_id:
        chat_url = FUNPAY_CHAT_URL.format(o.chat_id)
        txt += f"∟ Чат: <a href='{chat_url}'>Перейти</a>\n"
    return txt, o

def process_new_order(c, event):
    if not SETTINGS or not SETTINGS.enabled:
        return
    order = event.order
    if not order:
        return
    order_id = getattr(order, 'id', None)
    if not order_id:
        return

    logger.info(
        f"[ASRplus] НОВЫЙ ЗАКАЗ: id={order_id}, "
        f"buyer={getattr(order, 'buyer_username', '?')}, "
        f"buyer_id={getattr(order, 'buyer_id', '?')}, "
        f"chat_id={getattr(order, 'chat_id', '?')}, "
        f"quantity={getattr(order, 'quantity', '?')}, "
        f"offer_id={getattr(order, 'offer_id', '?')}, "
        f"lot_id={getattr(order, 'lot_id', '?')}, "
        f"description={str(getattr(order, 'description', '?'))[:80]}"
    )

    # Проверка: если заказ уже помечен как "чужой" (нет тега) — молча игнорируем
    with _ignored_lock:
        if order_id in _ignored_orders:
            logger.debug(f"[ASRplus] Заказ #{order_id} в списке игнорируемых (нет тега), пропуск")
            return

    with _processed_lock:
        if order_id in _processed_orders:
            logger.debug(f"[ASRplus] Заказ #{order_id} уже обрабатывается, пропуск")
            return
        _processed_orders[order_id] = time.time()

    # Проверка чёрного списка
    buyer_check = getattr(order, 'buyer_username', None) or getattr(order, 'buyer', '')
    if SETTINGS.is_blacklisted(buyer_check):
        logger.info(f"[ASRplus] Заказ #{order_id} от {buyer_check!r} — в чёрном списке, возврат")
        if _do_refund(c, order_id):
            chat_id_bl = getattr(order, 'chat_id', None) or getattr(order, 'node_id', 0)
            if chat_id_bl:
                _send_fp(c, chat_id_bl, "❌ К сожалению, мы не можем выполнить ваш заказ.")
            if tg_logs:
                tg_logs.refund(order_id, f"Чёрный список: {buyer_check}")
        with _processed_lock:
            _processed_orders.pop(order_id, None)
        return

    if order_id in ORDERS:
        logger.debug(f"[ASRplus] Заказ #{order_id} уже в ORDERS, пропуск")
        return
    _cleanup_processed()

    buyer    = getattr(order, 'buyer_username', None) or getattr(order, 'buyer', 'Unknown')
    buyer_id = int(getattr(order, 'buyer_id', 0) or 0)
    chat_id  = getattr(order, 'chat_id', None) or getattr(order, 'node_id', 0)
    quantity = int(getattr(order, 'quantity', None) or getattr(order, 'amount', None) or 0)
    description = getattr(order, 'description', None) or getattr(order, 'title', None) or "—"

    def _do_process():
        logger.info(
            f"[ASRplus] _do_process START: order_id={order_id}, "
            f"lots_configured={list(SETTINGS.lots.keys())}, "
            f"accounts_free={AccountRepo.count_free()}, "
            f"ORDERS_count={len(ORDERS)}"
        )
        processed_ok = False
        try:
            if order_id in ORDERS:
                logger.debug(f"[ASRplus] #{order_id}: уже в ORDERS при старте потока, выход")
                return
            
            _invalidate_lots_cache()
            lot_id = _find_lot_id_for_order(c, event)
            logger.info(f"[ASRplus] #{order_id}: _find_lot_id_for_order вернул: {lot_id!r}")
            if not lot_id:
                our_subs = _our_subcategory_ids()
                order_sub = _order_subcategory_id(order)
                is_our_section = bool(our_subs) and order_sub is not None and order_sub in our_subs
                if is_our_section:
                    logger.warning(
                        f"[ASRplus] #{order_id}: лот не опознан, НО подкатегория ({order_sub}) "
                        f"совпадает с одним из настроенных лотов — это похоже на наш заказ! "
                        f"Выдача отменена, требуется внимание. "
                        f"description={str(description)[:120]!r}, lots={list(SETTINGS.lots.keys())}"
                    )
                    if tg_logs:
                        tg_logs.error(
                            f"⚠️ Заказ #{str(order_id)[:12]}: наш раздел, но лот не опознан, "
                            f"выдача отменена. Описание: {str(description)[:120]}"
                        )
                else:
                    logger.warning(
                        f"[ASRplus] #{order_id}: лот не найден среди настроенных лотов — "
                        f"заказ НЕ относится к этому плагину. "
                        f"Помещаем в список игнорируемых на {IGNORED_ORDER_TTL // 60} мин. "
                        f"lots={list(SETTINGS.lots.keys())}"
                    )
                # Заказ либо чужой, либо наш-но-неопознанный: в обоих случаях аккаунт не выдаём.
                # Через IGNORED_ORDER_TTL секунд запись сама вылетит (не сохраняется в плагине).
                with _ignored_lock:
                    _ignored_orders[order_id] = time.time()
                with _processed_lock:
                    _processed_orders.pop(order_id, None)
                return

            lot_cfg = SETTINGS.get_lot(lot_id)
            if not lot_cfg:
                logger.warning(f"[ASRplus] #{order_id}: lot_cfg не найден для lot_id={lot_id}")
                # Тоже помечаем как игнорируемый — lot_cfg отсутствует, значит лот не настроен
                with _ignored_lock:
                    _ignored_orders[order_id] = time.time()
                with _processed_lock:
                    _processed_orders.pop(order_id, None)
                return

            
            tag = _ntag(lot_cfg.tag)
            if not tag or tag == "default" or not any(_ntag(a.tag) == tag for a in ACCOUNTS):
                # Тег из настроек лота пуст/не настроен — пробуем #тег из подробного описания лота как fallback
                hash_tag = _extract_tag_from_lot_description(c, lot_id)
                if hash_tag:
                    tag = hash_tag
                    logger.info(
                        f"[ASRplus] #{order_id}: тег из lot_cfg не задан/невалиден, "
                        f"взят #тег из подробного описания лота: #{tag}"
                    )
                else:
                    logger.info(f"[ASRplus] #{order_id}: тег из lot_cfg: {tag!r} (fallback #тег не найден)")
            else:
                logger.info(f"[ASRplus] #{order_id}: тег из lot_cfg: {tag!r}")

            # Сырой ID лота, с которого реально куплен заказ (без резолвинга через
            # тег к основному лоту) — нужен, чтобы понять: 1) с какого именно
            # варианта лота-продления (со своим временем) пришла оплата, и
            # 2) является ли заказ вообще покупкой лота-продления, а не обычного лота.
            try:
                raw_offer_id = _extract_raw_offer_id(c, event)
            except Exception:
                raw_offer_id = None

            if lot_cfg.lot_type == "fixed":
                # Фиксированный лот: количество купленных штук не влияет на время.
                # Время аренды берётся из настроек лота, НО если оплата пришла
                # с одного из лотов-продлений с явно заданным временем (вариант,
                # который выбрал покупатель командой !продлить), используем
                # именно его время, а не время основного лота.
                fixed_h = lot_cfg.fixed_hours
                eff_opts = _effective_extend_options(lot_cfg)
                if eff_opts and raw_offer_id:
                    opt = next((o for o in eff_opts
                                if str(o.get("lot_id")) == raw_offer_id), None)
                    if opt and opt.get("hours"):
                        fixed_h = float(opt["hours"])
                        logger.info(
                            f"[ASRplus] #{order_id}: заказ пришёл с лота-продления "
                            f"#{raw_offer_id}, время аренды = {fixed_h}ч (вариант выбора)"
                        )
                if not fixed_h or fixed_h <= 0:
                    fixed_h = 1
                    logger.warning(
                        f"[ASRplus] #{order_id}: у фиксированного лота {lot_id} не задано "
                        f"время (fixed_hours пусто/некорректно) — используем 1ч по умолчанию. "
                        f"Задайте время в настройках лота."
                    )
                hours = fixed_h
                logger.info(f"[ASRplus] #{order_id}: фиксированный лот {lot_id}, время={hours}ч (quantity={quantity} игнорируется)")
            else:
                hours = quantity if quantity > 0 else 1

            
            with _data_lock:
                existing = AccountRepo.find_active_by_buyer(buyer_id, tag)
                # Продлеваем существующую аренду ТОЛЬКО если этот заказ пришёл именно
                # с лота-продления (extend_lot_id), настроенного для исходного лота
                # покупателя. Раньше продление срабатывало при любом совпадении
                # lot_id (в т.ч. при повторной покупке того же обычного лота), из-за
                # чего покупка 2-го аккаунта той же категории ошибочно продлевала
                # первый вместо выдачи отдельного аккаунта. Теперь: покупка обычного
                # лота (даже совпадающего с первым) — это новый отдельный аккаунт;
                # продление срабатывает исключительно через лот-продление.
                is_extend_purchase = False
                if existing and existing.lot_id and raw_offer_id:
                    existing_lot_cfg = SETTINGS.get_lot(existing.lot_id)
                    if existing_lot_cfg:
                        eff_ext_id = _effective_extend_lot_id(existing_lot_cfg)
                        eff_ext_opts = _effective_extend_options(existing_lot_cfg)
                        if eff_ext_id and _ntag(eff_ext_id) == _ntag(raw_offer_id):
                            is_extend_purchase = True
                        elif eff_ext_opts and any(
                                _ntag(str(o.get("lot_id"))) == _ntag(raw_offer_id)
                                for o in eff_ext_opts):
                            is_extend_purchase = True
                if existing and not is_extend_purchase:
                    logger.info(
                        f"[ASRplus] #{order_id}: найдена активная аренда buyer_id={buyer_id} "
                        f"(existing_lot={existing.lot_id}, new_lot={lot_id}), но заказ пришёл НЕ "
                        f"с лота-продления — выдаём отдельный (второй) аккаунт, не продлеваем"
                    )
                    existing = None
                if existing and order_id not in ORDERS:
                    acc = AccountRepo.get(existing.acc_id)
                    if acc and acc.rental_end:
                        new_end = _fmt(_parse(acc.rental_end) + timedelta(hours=hours))
                        acc.rental_end = new_end
                        _save_accounts()
                        ORDERS[order_id] = RentOrder(
                            id=order_id, chat_id=chat_id, buyer=buyer, buyer_id=buyer_id,
                            acc_id=acc.id, acc_login=acc.login, acc_tag=_ntag(acc.tag),
                            hours=float(hours), status=RentStatus.ACTIVE,
                            is_extension=True, lot_id=lot_id)
                        _save_orders()
                        # ID лота-продления, с которого реально пришла оплата (а НЕ
                        # основной лот, к которому lot_id резолвится через тег) —
                        # именно его нужно выключать/отвязывать после покупки.
                        _bought_extend_id = raw_offer_id
                        # Отменяем таймер авто-выключения лот-продления — покупка прошла
                        _cancel_extend_lot_timer(_bought_extend_id or lot_id)
                        # Отключаем лот-продление обратно после покупки
                        if _bought_extend_id:
                            threading.Thread(
                                target=lambda: _toggle_single_lot(c, _bought_extend_id, False),
                                daemon=True
                            ).start()
                        else:
                            logger.warning(
                                f"[ASRplus] #{order_id}: не удалось определить ID купленного "
                                f"лота-продления для автоотключения (raw_offer_id/eff_ext_id пусты)"
                            )
                        _send_fp(c, chat_id, _tmpl(SETTINGS.messages.auto_extended,
                                                    hours=str(hours), end_time=new_end))
                        if tg_logs:
                            tg_logs.order_completed(ORDERS[order_id], acc.login)
                        processed_ok = True
                        logger.info(f"[ASRplus] #{order_id}: продлена аренда для buyer_id={buyer_id} тег={tag!r}")
                        return

            # Отправляем "подготавливается" только когда лот подтверждён и флаг включён
            if chat_id and SETTINGS.notification_preparing:
                try:
                    _send_fp(c, chat_id, (
                        f"⏳ Ваш заказ принят!\n\n"
                        f"∟ Заказ: #{order_id}\n"
                        f"∟ Товар: {description}\n"
                        f"∟ Время аренды (часов): {hours}\n\n"
                        f"🔄 Аккаунт подготавливается, пожалуйста подождите..."
                    ))
                except Exception as _e:
                    logger.warning(f"[ASRplus] preparing_msg #{order_id}: {_e}")

            logger.info(
                f"[ASRplus] #{order_id}: вызываю _assign_account("
                f"tag={tag!r}, lot_id={lot_id!r}, hours={hours}, buyer={buyer!r})"
            )
            _assign_account(c, order_id, tag, lot_id, buyer, buyer_id, chat_id, hours)
            processed_ok = True

        except Exception as e:
            code, desc, _ = _classify_error(e)
            logger.error(
                f"[ASRplus] КРИТИЧЕСКАЯ ОШИБКА обработки #{order_id}: [{code}] {desc} (raw: {e})",
                exc_info=True)
            if tg_logs:
                try:
                    tg_logs.error(f"Обработка заказа #{order_id[:12]}...\n∟ Причина: {desc}\n∟ Код: <code>{code}</code>")
                except Exception:
                    pass
        finally:
            if not processed_ok and order_id not in ORDERS:
                with _processed_lock:
                    _processed_orders.pop(order_id, None)
                logger.warning(f"[ASRplus] #{order_id}: обработка не завершена, "
                               f"заказ удалён из processed для возможного повтора")

    threading.Thread(target=_do_process, daemon=True, name=f"ASRplus-Order-{order_id}").start()

def _find_tag_from_order_description_text(description: str, order_id: Optional[str] = None) -> Optional[str]:
    """Ищет #тег прямо в строке описания без сетевых вызовов."""
    return _parse_hash_tag_from_text(description, exclude_id=order_id)

def _assign_account(c, order_id: str, tag: str, lot_id: Optional[str],
                    buyer: str, buyer_id: int, chat_id, hours: int):
    # ── Строгая проверка: тег и lot_id должны совпадать с нашей конфигурацией ──
    # Если lot_id не None — убеждаемся что он реально настроен и тег совпадает
    if lot_id is not None:
        lot_cfg = SETTINGS.get_lot(lot_id)
        if not lot_cfg:
            logger.warning(
                f"[ASRplus] _assign_account #{order_id}: lot_id={lot_id!r} не найден "
                f"в настроенных лотах — заказ скипается (не наш)"
            )
            return
        cfg_tag = _ntag(lot_cfg.tag)
        req_tag = _ntag(tag)
        if cfg_tag != req_tag:
            logger.warning(
                f"[ASRplus] _assign_account #{order_id}: тег из lot_cfg ({cfg_tag!r}) "
                f"не совпадает с запрошенным ({req_tag!r}) — заказ скипается"
            )
            return
    free_before = AccountRepo.count_free(tag).get(_ntag(tag), 0)
    logger.info(f"[ASRplus] #{order_id}: попытка выдачи аккаунта (тег={tag}, свободных={free_before})")

    # Проверяем лимит времени — фильтруем кандидатов у которых хватает лимита
    with _data_lock:
        for _cand in [a for a in ACCOUNTS if _ntag(a.tag) == _ntag(tag) and a.status == RentStatus.FREE]:
            if _cand.time_limit_hours is not None and hours > _cand.time_limit_hours:
                logger.info(f"[ASRplus] #{order_id}: аккаунт {_cand.login} пропущен — лимит {_cand.time_limit_hours}ч < {hours}ч")

    acc = AccountRepo.claim_free(tag, order_id, buyer, buyer_id, chat_id, hours)
    # Если попался аккаунт с лимитом меньше запрошенных часов — освобождаем и отказываем
    if acc and acc.time_limit_hours is not None and hours > acc.time_limit_hours:
        AccountRepo.release(acc.id)
        acc = None
        logger.info(f"[ASRplus] #{order_id}: все аккаунты тега {tag!r} имеют лимит меньше {hours}ч")
    if not acc:
        reason = f"Нет свободных аккаунтов (тег: {tag})"
        logger.warning(f"[ASRplus] Нет свободных аккаунтов для #{order_id} (тег: {tag})")
        if lot_id:
            _send_fp(c, chat_id, SETTINGS.messages.no_accounts)
        if _do_refund(c, order_id):
            if lot_id:
                _send_fp(c, chat_id, SETTINGS.messages.refunded)
            if tg_logs:
                tg_logs.refund(order_id, reason)
        else:
            if tg_logs:
                tg_logs.error(f"Нет аккаунтов для заказа #{order_id[:12]} (тег: {tag})")
        if SETTINGS.auto_disable_lots:
            def _disable(c=c, tag=tag):
                toggled = _toggle_fp_lots_for_tag(c, tag, False)
                if toggled and tg_logs:
                    tg_logs.lots_auto_disabled(tag, toggled)
            threading.Thread(target=_disable, daemon=True).start()
        return

    with _data_lock:
        ro = RentOrder(id=order_id, chat_id=chat_id, buyer=buyer, buyer_id=buyer_id,
                       acc_id=acc.id, acc_login=acc.login, acc_tag=_ntag(acc.tag),
                       hours=float(hours), lot_id=lot_id)
        ORDERS[order_id] = ro
        _save_orders()

    # Регистрируем в хранилище ожидания — хранится до TTL, подтверждается при первом !код
    _pending_store.add(
        order_id=order_id, buyer=buyer, buyer_id=buyer_id,
        chat_id=chat_id, tag=_ntag(acc.tag), lot_id=lot_id,
        hours=hours,
        ttl=max(float(hours) * 3600 + 3600, 7200.0)  # аренда + 1 час сверху, минимум 2 ч
    )

    end_time = acc.rental_end or "—"
    remaining_str = _remaining_str(acc.rental_end) if acc.rental_end else "—"
    _send_fp(c, chat_id, _tmpl(SETTINGS.messages.order_completed,
                                login=acc.login, password=acc.password, id=order_id,
                                hours=str(hours), end_time=end_time,
                                remaining=remaining_str,
                                code="", link="", stock_list="",
                                commands_list=BUYER_COMMANDS_TEXT))
    if tg_logs:
        tg_logs.order_completed(ro, acc.login)

    if SETTINGS.auto_disable_lots:
        free_remaining = AccountRepo.count_free(tag).get(_ntag(tag), 0)
        if free_remaining == 0:
            def _disable_after(c=c, tag=tag):
                toggled = _toggle_fp_lots_for_tag(c, tag, False)
                if toggled and tg_logs:
                    tg_logs.lots_auto_disabled(tag, toggled)
            threading.Thread(target=_disable_after, daemon=True).start()

def process_message(c, event):
    if not SETTINGS or not SETTINGS.enabled:
        return
    msg = event.message
    if not msg or not msg.text:
        return
    if msg.author_id == 0:
        if msg.type == MessageTypes.NEW_FEEDBACK:
            _handle_feedback(c, msg)
        return

    # Если у покупателя открыт выбор времени продления (лот типа "fixed" с
    # несколькими вариантами лотов-продлений) — пытаемся разобрать его ответ
    # как число часов, не дожидаясь совпадения с обычными командами.
    with _extend_choice_lock:
        pending = _extend_choice_pending.get(msg.chat_id)
        if pending and pending["expire"] < time.time():
            _extend_choice_pending.pop(msg.chat_id, None)
            pending = None
    if pending:
        raw_txt = msg.text.strip().replace(",", ".")
        try:
            hours_val = float(raw_txt)
        except ValueError:
            hours_val = None
        if hours_val is not None:
            opt = next((o for o in pending["options"]
                        if abs(float(o.get("hours", -1)) - hours_val) < 0.01), None)
            with _extend_choice_lock:
                _extend_choice_pending.pop(msg.chat_id, None)
            if not opt:
                available = ", ".join(_fmt_hours(o.get("hours")) for o in pending["options"])
                _send_fp(c, msg.chat_id,
                         f"❌ Такого варианта нет. Доступно: {available}. Напишите !продлить, чтобы попробовать снова.")
                return
            order = ORDERS.get(pending["order_id"])
            if not order:
                _send_fp(c, msg.chat_id, SETTINGS.messages.error_msg)
                return
            acc_for_remaining = AccountRepo.get(order.acc_id)
            remaining = _remaining_str(acc_for_remaining.rental_end) if acc_for_remaining and acc_for_remaining.rental_end else "—"
            link = _enable_extend_lot_target(c, order, str(opt.get("lot_id")))
            _send_fp(c, msg.chat_id, _tmpl(SETTINGS.messages.extend_link, link=link, remaining=remaining))
            return
        # Не число — пропускаем в обычную обработку команд (не считаем выбором),
        # но не удаляем ожидание, чтобы покупатель мог ответить позже в течение TTL.

    fl = msg.text.strip().split('\n', 1)[0].strip().lower()
    is_code = fl in _CMD_CODE
    is_time = fl in _CMD_TIME
    is_extend = fl in _CMD_EXTEND
    is_stock = fl in _CMD_STOCK
    is_account = fl in _CMD_ACCOUNT
    is_seller = fl in _CMD_SELLER
    if not (is_code or is_time or is_extend or is_stock or is_account or is_seller):
        return
    try:
        _process_buyer_command(c, event, msg, is_code, is_time, is_extend, is_stock, is_account, is_seller)
    except Exception as e:
        # Раньше необработанное исключение в любой из команд (например !продлить)
        # приводило к тому, что покупатель просто не получал ответа, а причина
        # оставалась не видна. Теперь ловим любую ошибку, классифицируем её,
        # логируем, шлём продавцу точную причину и отвечаем покупателю понятным
        # сообщением, не раскрывая ему технические детали.
        code, desc, _ = _classify_error(e)
        logger.error(
            f"[ASRplus] Ошибка обработки команды '{fl}' от чата {getattr(msg, 'chat_id', '?')}: "
            f"[{code}] {desc} (raw: {_safe_err(e)})"
        )
        if tg_logs:
            try:
                tg_logs.error(f"Команда «{fl}»\n∟ Причина: {desc}\n∟ Код: <code>{code}</code>")
            except Exception:
                pass
        try:
            _send_fp(c, msg.chat_id, SETTINGS.messages.error_msg)
        except Exception:
            pass

def _process_buyer_command(c, event, msg, is_code, is_time, is_extend, is_stock, is_account, is_seller):
    author_name = getattr(msg, 'author', None) or getattr(msg, 'author_username', None)
    author_id = getattr(msg, 'author_id', None) or 0
    if is_stock:
        tag = AccountRepo.find_tag_by_chat(msg.chat_id, author_id, author_name)
        _send_fp(c, msg.chat_id, _build_stock_message(tag))
        return
    if is_seller:
        cd_key = f"seller:{msg.chat_id}"
        now_ts = time.time()
        with _cooldowns_lock:
            if _code_cooldowns.get(cd_key, 0) > now_ts - SELLER_CALL_COOLDOWN:
                _send_fp(c, msg.chat_id, SETTINGS.messages.seller_call_cooldown)
                return
            _code_cooldowns[cd_key] = now_ts
        order_for_seller = AccountRepo.find_order_by_chat(msg.chat_id, author_id, author_name)
        buyer_display = author_name or (order_for_seller.buyer if order_for_seller else "неизвестен")
        order_id_for_seller = order_for_seller.id if order_for_seller else ""
        if tg_logs:
            tg_logs.seller_called(msg.chat_id, buyer_display, order_id_for_seller)
        _send_fp(c, msg.chat_id, SETTINGS.messages.seller_called)
        return
    if is_account:
        order = None
        if author_id and author_id > 0:
            active_orders = [
                o for o in ORDERS.values()
                if o.status == RentStatus.ACTIVE and o.buyer_id == author_id
            ]
            if active_orders:
                order = max(active_orders, key=lambda o: o.created_at)
        if not order:
            order = AccountRepo.find_order_by_chat(msg.chat_id, author_id, author_name)
        if not order or order.status != RentStatus.ACTIVE:
            _send_fp(c, msg.chat_id, SETTINGS.messages.no_order)
            return
        acc = AccountRepo.get(order.acc_id)
        if not acc:
            _send_fp(c, msg.chat_id, SETTINGS.messages.no_account)
            # Освобождаем "зависший" заказ, чтобы авто-отключение лотов работало корректно
            with _data_lock:
                order.update(status=RentStatus.ERROR)
            logger.warning(
                f"[ASRplus] !аккаунт: acc_id={order.acc_id} не найден для заказа #{order.id}, "
                f"заказ переведён в ERROR"
            )
            return
        if order.chat_id != msg.chat_id:
            order.update(chat_id=msg.chat_id)
        if acc.owner_chat_id != msg.chat_id:
            with _data_lock:
                acc.owner_chat_id = msg.chat_id
                _save_accounts()
        end_time = acc.rental_end or "—"
        remaining_str = _remaining_str(acc.rental_end) if acc.rental_end else "—"
        _send_fp(c, msg.chat_id, _tmpl(SETTINGS.messages.order_completed,
                                        login=acc.login, password=acc.password, id=order.id,
                                        hours=str(int(order.hours)), end_time=end_time,
                                        remaining=remaining_str,
                                        code="", link="", stock_list="",
                                        commands_list=BUYER_COMMANDS_TEXT))
        if tg_logs and SETTINGS.notification_order_completed:
            tg_logs._send(f"🔄 Повторная выдача по !аккаунт\n∟ Покупатель: {order.buyer}\n∟ Аккаунт: {acc.login}\n∟ Заказ: #{order.id[:12]}...")
        return
    if is_code and author_id and author_id > 0:
        # Если у покупателя одновременно несколько активных аккаунтов (купил
        # 2+ аккаунта одной категории) — по команде !код выдаём коды сразу по
        # всем, в формате "Логин : код" (без пароля), одной строкой на аккаунт.
        active_orders_multi = [
            o for o in ORDERS.values()
            if o.status == RentStatus.ACTIVE and o.buyer_id == author_id
        ]
        seen_acc_ids = set()
        multi_pairs = []
        for o in sorted(active_orders_multi, key=lambda x: x.created_at):
            if o.acc_id in seen_acc_ids:
                continue
            acc_o = AccountRepo.get(o.acc_id)
            if acc_o and acc_o.status == RentStatus.ACTIVE and acc_o.current_order == o.id:
                multi_pairs.append((o, acc_o))
                seen_acc_ids.add(o.acc_id)
        if len(multi_pairs) >= 2:
            cd_key = str(msg.chat_id)
            now_ts = time.time()
            with _cooldowns_lock:
                if _code_cooldowns.get(cd_key, 0) > now_ts - CODE_COOLDOWN:
                    return
                _code_cooldowns[cd_key] = now_ts
            lines = []
            for o, acc_o in multi_pairs:
                ss = acc_o.mafile.get("shared_secret", "")
                if not ss:
                    continue
                code = SteamGuard.code_sync(ss)
                if code in ("ERROR", "NO_SECRET"):
                    continue
                lines.append(f"{acc_o.login} : {code}")
                with _data_lock:
                    acc_o.access_count += 1
                if o.chat_id != msg.chat_id:
                    o.update(chat_id=msg.chat_id)
                if acc_o.owner_chat_id != msg.chat_id:
                    acc_o.owner_chat_id = msg.chat_id
                _pending_store.confirm(o.id)
            with _data_lock:
                _save_accounts()
            _pending_store.confirm_by_buyer(author_id, author_name or "")
            if lines:
                _send_fp(c, msg.chat_id, "\n".join(lines))
            else:
                _send_fp(c, msg.chat_id, SETTINGS.messages.code_error)
            return

    order = AccountRepo.find_order_by_chat(msg.chat_id, author_id, author_name)
    if not order:
        _send_fp(c, msg.chat_id, SETTINGS.messages.no_order)
        return
    if order.status != RentStatus.ACTIVE:
        _send_fp(c, msg.chat_id, SETTINGS.messages.no_order)
        return
    acc = AccountRepo.get(order.acc_id)
    if not acc:
        _send_fp(c, msg.chat_id, SETTINGS.messages.no_account)
        return
    if order.chat_id != msg.chat_id:
        order.update(chat_id=msg.chat_id)
    if acc.owner_chat_id != msg.chat_id:
        with _data_lock:
            acc.owner_chat_id = msg.chat_id
            _save_accounts()
    if is_code:
        cd_key = str(msg.chat_id)
        now_ts = time.time()
        with _cooldowns_lock:
            if _code_cooldowns.get(cd_key, 0) > now_ts - CODE_COOLDOWN:
                return
            _code_cooldowns[cd_key] = now_ts
        ss = acc.mafile.get("shared_secret", "")
        if not ss:
            _send_fp(c, msg.chat_id, SETTINGS.messages.config_error)
            return
        code = SteamGuard.code_sync(ss)
        if code in ("ERROR", "NO_SECRET"):
            _send_fp(c, msg.chat_id, SETTINGS.messages.code_error)
            return
        end_time_str = acc.rental_end
        if not end_time_str:
            if order and hasattr(order, 'hours') and order.hours:
                try:
                    recovered_end = _fmt(_now() + timedelta(hours=float(order.hours)))
                    with _data_lock:
                        acc.rental_end = recovered_end
                        _save_accounts()
                    end_time_str = recovered_end
                    logger.warning(f"[ASRplus] rental_end был None для acc_id={acc.id}, восстановлен: {recovered_end}")
                except Exception:
                    pass
        _send_fp(c, msg.chat_id, _tmpl(SETTINGS.messages.guard_code,
                                        code=code, end_time=end_time_str or "неизвестно"))
        with _data_lock:
            acc.access_count += 1
            _save_accounts()
        # Отмечаем заказ как подтверждённый покупателем в хранилище ожидания
        _pending_store.confirm(order.id)
        _pending_store.confirm_by_buyer(author_id, author_name or "")
    elif is_time:
        if not acc.rental_end:
            _send_fp(c, msg.chat_id, SETTINGS.messages.rent_not_started)
        elif (_parse(acc.rental_end) - _now()).total_seconds() <= 0:
            _send_fp(c, msg.chat_id, SETTINGS.messages.rent_expired)
        else:
            _send_fp(c, msg.chat_id, _tmpl(SETTINGS.messages.time_info,
                                            remaining=_remaining_str(acc.rental_end),
                                            end_time=acc.rental_end))
    elif is_extend:
        targets = _get_buyer_active_targets(author_id) if author_id and author_id > 0 else []
        if len(targets) >= 2:
            # У покупателя несколько активных аккаунтов — присылаем персональную
            # ссылку на продление для каждого, чтобы не продлить не тот аккаунт.
            lines = []
            for o, acc_o in targets:
                link = _enable_extend_lot(c, o)
                if link:
                    lines.append(f"{acc_o.login} - {link}")
                else:
                    lines.append(f"{acc_o.login} - продление недоступно, обратитесь к продавцу")
            _send_fp(c, msg.chat_id, _tmpl(SETTINGS.messages.warning_multi, accounts_list="\n".join(lines)))
            return

        lot_cfg = SETTINGS.get_lot(order.lot_id) if order.lot_id else None

        # Для лотов с фикс. временем и несколькими вариантами продления —
        # спрашиваем покупателя, на сколько часов он хочет продлить.
        # Варианты берём индивидуальные лота, а если не заданы — общие для тега.
        effective_options = _effective_extend_options(lot_cfg) if lot_cfg and lot_cfg.lot_type == "fixed" else []
        if effective_options:
            options = effective_options
            lines = "\n".join(f"{_fmt_hours(o.get('hours'))} час(ов)" for o in options)
            with _extend_choice_lock:
                _extend_choice_pending[msg.chat_id] = {
                    "order_id": order.id,
                    "options": options,
                    "expire": time.time() + EXTEND_CHOICE_TIMEOUT,
                }
            _send_fp(c, msg.chat_id,
                     f"Доступные варианты продления, напишите число часов в чат:\n{lines}")
            return

        extend_lot_id = _effective_extend_lot_id(lot_cfg)

        if not extend_lot_id:
            # Лот-продление не настроен продавцом для этого лота — сообщаем и выходим,
            # не пытаясь угадывать lot_id (раньше здесь был вызов несуществующей
            # функции _get_extend_lot_id, из-за чего команда падала с ошибкой).
            _send_fp(c, msg.chat_id, SETTINGS.messages.extend_no_lot)
            return

        remaining = _remaining_str(acc.rental_end) if acc.rental_end else "—"
        link = _enable_extend_lot(c, order)
        _send_fp(c, msg.chat_id, _tmpl(SETTINGS.messages.extend_link, link=link, remaining=remaining))

def _handle_feedback(c, message):
    # Бонус только за 5 звёзд
    stars = None
    for attr in ("review_stars", "stars", "rating", "vote"):
        val = getattr(message, attr, None)
        if val is not None:
            try:
                stars = int(val)
            except Exception:
                pass
            break
    if stars is not None and stars < 5:
        return
    try:
        from FunPayAPI.common.utils import RegularExpressions
        oids = RegularExpressions().ORDER_ID.findall(message.text or "")
    except Exception:
        return
    if not oids:
        return
    oid = oids[0].replace("#", "")
    order = ORDERS.get(oid)
    if not order or order.review_claimed:
        return
    bonus = SETTINGS.get_bonus_for_hours(order.hours)
    if bonus > 0:
        ne = AccountRepo.extend_rent(order.acc_id, bonus)
        if ne:
            order.update(review_claimed=True)
            _send_fp(c, order.chat_id, _tmpl(SETTINGS.messages.bonus, hours=str(bonus)))

def process_order_status_changed(c, event):
    if not SETTINGS.enabled or event.order.status not in (OrderStatuses.CLOSED, OrderStatuses.REFUNDED):
        return
    # Если заказ в списке игнорируемых (нет тега) — не трогаем
    with _ignored_lock:
        if event.order.id in _ignored_orders:
            logger.debug(f"[ASRplus] order_status_changed #{event.order.id} — в игнорируемых, пропуск")
            return
    order = ORDERS.get(event.order.id)
    if not order or order.status in (RentStatus.FINISHED, RentStatus.REFUND):
        return
    if event.order.status == OrderStatuses.REFUNDED:
        acc = AccountRepo.by_order(event.order.id) or AccountRepo.get(order.acc_id)
        if acc:
            with _recovering_lock:
                if acc.id in _recovering_accounts:
                    return
                _recovering_accounts.add(acc.id)
            def _do_refund_recover(a=acc, o=order):
                try:
                    _recover_account(c, a, o, "REFUND_EXT")
                finally:
                    with _recovering_lock:
                        _recovering_accounts.discard(a.id)
            threading.Thread(target=_do_refund_recover, daemon=True).start()
    elif event.order.status == OrderStatuses.CLOSED:
        # ВАЖНО (фикс бага): FunPay переводит заказ в CLOSED, когда покупатель
        # подтвердил заказ вручную (или это произошло автоматически). Раньше
        # это сразу переводило RentOrder в FINISHED, из-за чего команды
        # !код/!time/!аккаунт переставали работать, хотя аренда ещё активна.
        # Теперь: если аренда ещё активна — заказ остаётся ACTIVE (только
        # помечаем buyer_confirmed=True для статистики/логов), а реальное
        # завершение произойдёт в rental_check_loop -> _recover_account,
        # когда фактическое время аренды истечёт. Если же аренда уже не
        # активна (аккаунт не в статусе ACTIVE на этом заказе, либо время
        # аренды уже вышло) — тогда действительно можно закрыть заказ.
        acc = AccountRepo.by_order(event.order.id) or AccountRepo.get(order.acc_id)
        rent_still_active = False
        if acc and acc.status == RentStatus.ACTIVE and acc.current_order == order.id:
            if acc.rental_end:
                rent_still_active = (_parse(acc.rental_end) - _now()).total_seconds() > 0
            else:
                # rental_end почему-то не проставлен — считаем аренду активной,
                # чтобы не отрубать команды раньше времени; rental_check_loop
                # сам восстановит rental_end при следующем !код.
                rent_still_active = True
        if rent_still_active:
            order.update(buyer_confirmed=True)
            logger.debug(
                f"[ASRplus] Заказ #{order.id} подтверждён покупателем (CLOSED), "
                f"но аренда ещё активна — статус ACTIVE сохранён, команды продолжают работать"
            )
        else:
            order.update(status=RentStatus.FINISHED, buyer_confirmed=True)

_recovering_accounts: Set[int] = set()
_recovering_lock = threading.Lock()

# Словарь активных таймеров лот-продления: {extend_lot_id: threading.Timer}
_extend_lot_timers: Dict[str, threading.Timer] = {}
_extend_lot_timers_lock = threading.Lock()

EXTEND_LOT_TIMEOUT = 300  # 5 минут — потом лот-продление отключится если не купили

def _schedule_extend_lot_disable(c, extend_lot_id: str, order_id: str = ""):
    """Запускает таймер: через 5 минут отключит лот-продление если не пришёл новый заказ."""
    def _do_disable():
        with _extend_lot_timers_lock:
            _extend_lot_timers.pop(extend_lot_id, None)
        try:
            _toggle_single_lot(c, extend_lot_id, False)
            logger.info(f"[ASRplus] Лот-продление #{extend_lot_id} отключён по таймеру (никто не купил)")
            if tg_logs:
                tg_logs._send(
                    f"⏱ Лот-продление <code>#{extend_lot_id}</code> отключён автоматически\n"
                    f"∟ Покупатель не оплатил в течение {EXTEND_LOT_TIMEOUT//60} мин."
                )
        except Exception as e:
            logger.warning(f"[ASRplus] Ошибка авто-отключения лот-продления #{extend_lot_id}: {e}")

    with _extend_lot_timers_lock:
        # Отменяем предыдущий таймер если есть
        old = _extend_lot_timers.pop(extend_lot_id, None)
        if old:
            try:
                old.cancel()
            except Exception:
                pass
        t = threading.Timer(EXTEND_LOT_TIMEOUT, _do_disable)
        t.daemon = True
        _extend_lot_timers[extend_lot_id] = t
        t.start()
    logger.info(f"[ASRplus] Запущен таймер отключения лот-продления #{extend_lot_id} ({EXTEND_LOT_TIMEOUT}с)")

def _cancel_extend_lot_timer(extend_lot_id: str):
    """Отменяет таймер когда покупатель успел оплатить."""
    with _extend_lot_timers_lock:
        t = _extend_lot_timers.pop(extend_lot_id, None)
        if t:
            try:
                t.cancel()
            except Exception:
                pass
            logger.info(f"[ASRplus] Таймер лот-продления #{extend_lot_id} отменён (куплено)")

import queue as _queue

_order_queue: _queue.Queue = _queue.Queue()
_order_worker_thread: Optional[threading.Thread] = None
_order_worker_lock = threading.Lock()

def _order_worker(c):
    logger.info("[ASRplus] OrderWorker запущен")
    while not _stop_event.is_set():
        try:
            task = _order_queue.get(timeout=2)
        except _queue.Empty:
            continue
        try:
            fn, args, kwargs = task
            fn(*args, **kwargs)
        except Exception as e:
            logger.error(f"[ASRplus] OrderWorker ошибка задачи: {e}", exc_info=True)
        finally:
            _order_queue.task_done()
    logger.info("[ASRplus] OrderWorker остановлен")

def _ensure_order_worker(c):
    global _order_worker_thread
    with _order_worker_lock:
        if _order_worker_thread is None or not _order_worker_thread.is_alive():
            _order_worker_thread = threading.Thread(
                target=_order_worker, args=(c,), daemon=True, name="ASRplus-OrderWorker")
            _order_worker_thread.start()
            logger.info("[ASRplus] OrderWorker (пере)запущен")

def _worker_watchdog(c):
    while not _stop_event.is_set():
        _ensure_order_worker(c)
        _stop_event.wait(30)

def rental_check_loop(c):
    cleanup_counter = 0
    while not _stop_event.is_set():
        try:
            now = _now()
            with _data_lock:
                accounts_snapshot = list(ACCOUNTS)
            for acc in accounts_snapshot:
                if _stop_event.is_set():
                    return
                with _data_lock:
                    acc_status = acc.status
                    acc_order_id = acc.current_order
                    acc_rental_end = acc.rental_end
                    acc_id = acc.id
                if acc_status != RentStatus.ACTIVE or not acc_order_id:
                    continue
                order = ORDERS.get(acc_order_id)
                if not order:
                    AccountRepo.release(acc_id)
                    continue
                if acc_rental_end:
                    rem = (_parse(acc_rental_end) - now).total_seconds()
                    if 0 < rem < 600 and not order.warned:
                        try:
                            _notify_rent_ending_soon(c, order)
                        except Exception as e:
                            logger.error(f"[ASRplus] Ошибка отправки предупреждения #{order.id}: {_safe_err(e)}")
                            order.update(warned=True)  # чтобы не спамить, если ошибка повторяющаяся
                    if rem <= 0:
                        with _recovering_lock:
                            if acc_id in _recovering_accounts:
                                continue
                            _recovering_accounts.add(acc_id)
                        with _data_lock:
                            acc_snapshot = AccountRepo.get(acc_id)
                        order_snapshot = order
                        def _do_recover(a=acc_snapshot, o=order_snapshot):
                            try:
                                _recover_account(c, a, o, "TIME")
                            finally:
                                with _recovering_lock:
                                    _recovering_accounts.discard(a.id)
                        threading.Thread(target=_do_recover, daemon=True).start()
        except Exception as e:
            logger.error(f"[ASRplus] rental_check_loop ошибка: {e}")
        cleanup_counter += 1
        if cleanup_counter >= 10:
            cleanup_counter = 0
            _cleanup_orders()
            _cleanup_cooldowns()
            _cleanup_processed()
            expired = _pending_store.cleanup_expired()
            if expired:
                logger.debug(f"[ASRplus] PendingStore: очищено {expired} истёкших записей")
        _stop_event.wait(60)

class CBT:
    SP = f'{_CBT.PLUGIN_SETTINGS}:{UUID}'
    MAIN = "asr_main"
    CONFIG = "asr_config"
    ACC_MENU = "asr_accs"
    ACC_ADD = "asr_add"
    ACC_DEL = "asr_del"
    ACC_DEL_CONFIRM = "asr_adlcf"
    ACC_DEL_YES = "asr_adlyes"
    ACC_DEL_NO = "asr_adlno"
    ACC_LIST = "asr_lst"
    ACC_DETAIL = "asr_det"
    ACC_CODE = "asr_code"
    ACC_STOP = "asr_stop"
    ACC_CHPWD = "asr_chpwd"
    ACC_EXTEND = "asr_ext"
    ACC_EXTEND_DO = "asr_extdo"
    ACC_MANUAL = "asr_man"
    ACC_MANUAL_HOURS = "asr_manhr"
    ACC_RESET = "asr_rst"
    ACC_SET_PWD = "asr_setpwd"
    ACC_EDIT_MAFILE = "asr_editma"
    LOTS = "asr_lots"
    LOTS_HOURLY = "asr_lotsh"
    LOTS_FIXED = "asr_lotsf"
    LOT_ADD = "asr_ladd"
    LOT_TAG = "asr_ltag"
    LOT_DETAIL = "asr_ldet"
    LOT_AUTO_TAG = "asr_lautotag"
    LOT_EDIT = "asr_ledt"
    LOT_EDIT_TAG = "asr_letag"
    LOT_RENAME = "asr_lren"
    LOT_DEL_CONFIRM = "asr_ldlcf"
    LOT_DEL_YES = "asr_ldlyes"
    LOT_DEL_NO = "asr_ldlno"
    LOT_TOGGLE_FP = "asr_ltglfp"
    LOTS_DISABLE_ALL = "asr_ldisall"
    LOTS_ENABLE_ALL = "asr_lenall"
    REVS = "asr_revs"
    REV_ADD = "asr_radd"
    REV_DEL = "asr_rdel"
    REV_HRS = "asr_rhrs"
    REV_BON = "asr_rbon"
    NOTIFS = "asr_ntf"
    MSGS = "asr_msgs"
    MSG_EDIT = "asr_medt"
    MSG_RESET = "asr_mrst"
    STATS = "asr_stat"
    FULL_STATS = "asr_fstat"
    HIST = "asr_hist"
    HIST_DETAIL = "asr_hdet"
    TOGGLE = "asr_tgl"
    FILES = "asr_files"
    FILES_CONFIRM = "asr_files_yes"
    ACTIVE_RENTS = "asr_active"
    PENDING_ORDERS = "asr_pending"
    ACC_BY_TAG = "asr_bytag"
    ACC_SEARCH = "asr_search"
    FREE_ACCS = "asr_free"
    HIST_CLEAR = "asr_hclr"
    HIST_CLEAR_YES = "asr_hclryes"
    HIST_CLEAR_NO = "asr_hclrno"
    FUNCTIONS = "asr_func"
    BLACKLIST = "asr_bl"
    BLACKLIST_ADD = "asr_bladd"
    BLACKLIST_DEL = "asr_bldel"
    ACC_SET_LIMIT = "asr_setlim"
    LOT_NOTE = "asr_lnote"
    LOT_FIXED_HOURS = "asr_lfxh"
    TOP_TAGS = "asr_toptags"
    BULK_ACCS = "asr_bulk"
    BULK_UPLOAD = "asr_bulk_up"
    BULK_DOWNLOAD = "asr_bulk_dl"
    BULK_CONFIRM = "asr_bulk_cf"
    LOT_EXTEND_LOT = "asr_lexlot"
    LOT_EXTEND_LOT_SET = "asr_lexlotset"
    LOT_EXTEND_LOT_DEL = "asr_lexlotdel"
    LOT_EXTOPT_ADD = "asr_lexoadd"
    LOT_EXTOPT_DEL = "asr_lexodel"
    TAG_EXTEND = "asr_texlist"
    TAG_EXTEND_DETAIL = "asr_texdet"
    TAG_EXTEND_LOT_SET = "asr_texlset"
    TAG_EXTEND_LOT_DEL = "asr_texldel"
    TAG_EXTOPT_ADD = "asr_texoadd"
    TAG_EXTOPT_DEL = "asr_texodel"
    ABOUT = "asr_about"
    PWD_BACKUPS = "asr_pwdbk"
    PWD_BACKUP_ACC = "asr_pwdbkacc"
    PWD_BACKUP_DL_ALL = "asr_pwdbkdlall"
    PWD_BACKUP_DL_ACC = "asr_pwdbkdlacc"

class States:
    LOGIN = "ASR_LOGIN"
    PASS = "ASR_PASS"
    TAG = "ASR_TAG"
    MAFILE = "ASR_MAFILE"
    MAN_BUYER = "ASR_MAN_BUYER"
    LOT_ID = "ASR_LOT_ID"
    LOT_RENAME = "ASR_LOT_RENAME"
    LOT_NOTE = "ASR_LOT_NOTE"
    LOT_FIXED_HOURS = "ASR_LOT_FIXED_HOURS"
    LOT_FIXED_HOURS_ADD = "ASR_LOT_FIXED_HOURS_ADD"
    MSG_EDIT = "ASR_MSG_EDIT"
    SET_PWD = "ASR_SET_PWD"
    EDIT_MAFILE = "ASR_MAFILE_EDIT"
    ACC_SEARCH = "ASR_ACC_SEARCH"
    MAN_HOURS = "ASR_MAN_HOURS"
    SET_EXTEND_LOT = "ASR_SET_EXTEND_LOT"
    BLACKLIST_ADD = "ASR_BL_ADD"
    SET_LIMIT = "ASR_SET_LIMIT"
    REV_HRS_CUSTOM = "ASR_REV_HRS_CUSTOM"
    REV_BON_CUSTOM = "ASR_REV_BON_CUSTOM"
    BULK_MAFILE = "ASR_BULK_MAFILE"
    EXTEND_LOT_ID = "ASR_EXTEND_LOT_ID"
    EXTOPT_LOT_ID = "ASR_EXTOPT_LOT_ID"
    EXTOPT_HOURS = "ASR_EXTOPT_HOURS"
    TAG_EXTEND_LOT_ID = "ASR_TAG_EXTEND_LOT_ID"
    TAG_EXTOPT_LOT_ID = "ASR_TAG_EXTOPT_LOT_ID"
    TAG_EXTOPT_HOURS = "ASR_TAG_EXTOPT_HOURS"

def _startup_diagnostics():
    issues = []
    if not ACCOUNTS:
        issues.append("Аккаунты не добавлены")
    else:
        bad_mafile = [a.login for a in ACCOUNTS if _validate_mafile(a.mafile)]
        if bad_mafile:
            issues.append(f"Неполный maFile: {', '.join(bad_mafile[:3])}")
    if not SETTINGS.lots:
        issues.append("Лоты не настроены")
    try:
        from playwright.sync_api import sync_playwright as _spw
        with _spw() as _p:
            if not os.path.exists(_p.chromium.executable_path):
                issues.append("Chromium не установлен — смена пароля может не работать")
    except Exception:
        issues.append("Playwright недоступен — смена пароля может не работать")
    if issues:
        logger.warning("[ASRplus] Диагностика:\n" + "\n".join(f"  ⚠️ {i}" for i in issues))
    else:
        logger.info("[ASRplus] Диагностика: OK")

def _toggle_single_lot(c, lot_id: str, enable: bool) -> bool:
    try:
        lf = c.account.get_lot_fields(int(lot_id))
        if lf.active != enable:
            lf.active = enable
            c.account.save_lot(lf)
            logger.debug(f"[ASRplus] Лот-продление #{lot_id} {'включён' if enable else 'выключен'}")
        _invalidate_lots_cache()
        return True
    except Exception as e:
        logger.warning(f"[ASRplus] Ошибка переключения лота-продления #{lot_id}: {e}")
        return False

def init(card: Cardinal):
    global cardinal_ref, tg_logs
    cardinal_ref = card
    tg_logs = TgLogs(card)
    SteamGuard.sync_time_sync()
    _startup_diagnostics()
    if _PY_VER_WARNING:
        tg_logs._send(
            f"⚠️ <b>Внимание: неподходящая версия Python</b>\n"
            f"∟ Обнаружено: <code>{_PY_VER_STR}</code>\n"
            f"∟ Требуется: <code>3.11.x</code>\n\n"
            f"<i>Плагин может работать нестабильно или с ошибками. "
            f"Рекомендуется обновиться до Python 3.11.</i>"
        )
    if not card.telegram:
        threading.Thread(target=rental_check_loop, args=(card,), daemon=True).start()
        _ensure_order_worker(card)
        threading.Thread(target=_worker_watchdog, args=(card,), daemon=True, name="ASRplus-Watchdog").start()
        logger.info("[ASRplus] Worker + Watchdog запущены при старте")
        return
    tg, bot = card.telegram, card.telegram.bot

    def send(cid, text, kb=None):
        real_id = cid.chat.id if hasattr(cid, 'chat') else cid
        return bot.send_message(real_id, text, reply_markup=kb, parse_mode='HTML')

    def edit(msg_or_cb, text, kb=None):
        try:
            if hasattr(msg_or_cb, 'chat'):
                return bot.edit_message_text(text, msg_or_cb.chat.id, msg_or_cb.message_id,
                                             reply_markup=kb, parse_mode='HTML')
            elif hasattr(msg_or_cb, 'message'):
                return bot.edit_message_text(text, msg_or_cb.message.chat.id, msg_or_cb.message.message_id,
                                             reply_markup=kb, parse_mode='HTML')
        except Exception:
            pass

    def answer(cb, msg=None, alert=False):
        try:
            return bot.answer_callback_query(cb.id, msg, show_alert=alert)
        except Exception:
            pass

    def _p(c, idx=-1):
        return c.data.split(":")[idx]

    def _pid(c, idx=-1):
        return int(_p(c, idx))

    def _back_kb(cb=None):
        return K().add(B("⬅️ Назад", None, cb or CBT.MAIN))

    def _send_txt_file(chat_id, fname, content, caption):
        data = content.encode("utf-8")
        try:
            bot.send_document(chat_id, (fname, data), caption=caption, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"[ASRplus] Ошибка отправки {fname}: {e}")
            bot.send_message(chat_id, f"❌ Не удалось отправить {fname}: {_safe_err(e)}", parse_mode="HTML")

    def _ask(chat_id, user_id, state, text, kb=None):
        msg = bot.send_message(chat_id, text, reply_markup=kb, parse_mode='HTML')
        _temp_storage.setdefault(user_id, {})["bot_msg_id"] = msg.message_id
        tg.set_state(chat_id, msg.message_id, user_id, state, {})
        return msg.message_id

    def _cleanup_dialog(chat_id, user_id, user_msg_id):
        d = _temp_storage.get(user_id, {})
        bot_msg_id = d.get("bot_msg_id")
        tg.clear_state(chat_id, user_id, False)
        if bot_msg_id:
            try:
                bot.delete_message(chat_id, bot_msg_id)
            except Exception:
                pass
        if user_msg_id:
            try:
                bot.delete_message(chat_id, user_msg_id)
            except Exception:
                pass
        if "bot_msg_id" in d:
            del d["bot_msg_id"]

    def _clear_state(user_id):
        d = _temp_storage.get(user_id, {})
        bot_msg_id = d.get("bot_msg_id")
        if bot_msg_id:
            try:
                bot.delete_message(d.get("chat_id"), bot_msg_id)
            except Exception:
                pass
        if user_id in _temp_storage:
            del _temp_storage[user_id]

    def _main_text():
        s = AccountRepo.get_stats()
        active = sum(1 for o in ORDERS.values() if o.status == RentStatus.ACTIVE)
        return (
            f"<b>⚡ ASR+ <code>v{VERSION}</code></b>\n"
            f"<i>Автоматическая аренда Steam аккаунтов</i>\n\n"
            f"∟ 🗄 Аккаунтов: <code>{s['total']}</code> "
            f"(🟢 {s[RentStatus.FREE]} своб. · 👤 {s[RentStatus.ACTIVE]} в аренде · ❌ {s[RentStatus.ERROR]} ошибок)\n"
            f"∟ 🔗 Лотов: <code>{len(SETTINGS.lots)}</code>\n"
            f"∟ 📦 Активных аренд: <code>{active}</code>\n"
        )

    def _main_kb():
        kb = K(row_width=1)
        kb.row(B(f"{_is_on(SETTINGS.enabled)} Авто-выдача", None, f"{CBT.TOGGLE}:enabled"))
        kb.row(B("📂 Аккаунты", None, CBT.ACC_MENU), B("🔗 Лоты", None, CBT.LOTS))
        kb.row(B("⭐️ Бонусы за отзывы", None, CBT.REVS), B("📊 Статистика", None, CBT.STATS))
        kb.row(B("🛠 Функции", None, CBT.FUNCTIONS))
        kb.row(B("📁 Получить файлы", None, f"{CBT.FILES}:all"))
        kb.row(B("ℹ️ О плагине", None, CBT.ABOUT))
        kb.row(B("⬅️ Назад", None, f"{_CBT.EDIT_PLUGIN}:{UUID}:0"))
        return kb

    def _config_kb():
        kb = K(row_width=1)
        kb.row(B(f"{_is_on(SETTINGS.auto_disable_lots)} Авто-выкл лотов при пустом складе", None, f"{CBT.TOGGLE}:auto_disable_lots"))
        kb.row(B(f"{_is_on(SETTINGS.auto_enable_lots)} Авто-вкл лотов при появлении аккаунтов", None, f"{CBT.TOGGLE}:auto_enable_lots"))
        kb.row(B(f"{_is_on(SETTINGS.autoback_on_error)} АВТО-ВОЗВРАТ", None, f"{CBT.TOGGLE}:autoback_on_error"))
        kb.row(B(f"{_is_on(SETTINGS.auto_free_on_error)} АВТО-FREE", None, f"{CBT.TOGGLE}:auto_free_on_error"))
        kb.row(B(f"{_is_on(SETTINGS.save_deleted_acc)} Сохранить данные", None, f"{CBT.TOGGLE}:save_deleted_acc"))
        kb.add(B("⬅️ Назад", None, CBT.FUNCTIONS))
        return kb

    def open_main(c):
        edit(c.message, _main_text(), _main_kb())

    def open_main_cmd(m):
        send(m.chat.id, _main_text(), _main_kb())

    def open_about(c):
        answer(c)
        # Показываем инструкцию по лотам если нажата соответствующая кнопка
        if hasattr(c, 'data') and c.data == f"{CBT.ABOUT}:howto":
            howto_text = (
                "📖 <b>Инструкция: как работают лоты</b>\n\n"
                "<b>Что такое лот?</b>\n"
                "Лот — это ваш товар на FunPay. Покупатель оплачивает лот, "
                "плагин автоматически выдаёт ему Steam аккаунт в аренду.\n\n"
                "<b>Как настроить лот:</b>\n"
                "1️⃣ Создайте лот на FunPay и скопируйте его ID из URL\n"
                "   <i>Пример URL: funpay.com/lots/offer?id=<b>123456</b></i>\n\n"
                "2️⃣ Перейдите в ASR+ → Лоты → Добавить лот\n"
                "3️⃣ Вставьте ID или ссылку на лот\n"
                "4️⃣ Выберите тег — плагин выдаёт аккаунты с этим тегом\n\n"
                "<b>Что такое тег?</b>\n"
                "Тег — это метка, которая связывает лот с нужными аккаунтами. "
                "Например: <code>cs2</code>, <code>pubg</code>, <code>default</code>.\n"
                "У каждого аккаунта есть свой тег — плагин выдаёт аккаунт, "
                "тег которого совпадает с тегом лота.\n\n"
                "<b>🆔 ID лота — точное опознавание лота в заказе:</b>\n"
                "Каждому настроенному лоту плагин автоматически создаёт СВОЙ уникальный "
                "служебный <code>#ID</code> (например <code>#id7k2p9a</code>) — он "
                "отдельный от обычного тега аккаунта и никогда не повторяется у двух лотов, "
                "даже если у них один и тот же тег/пул аккаунтов.\n"
                "✅ При добавлении НОВОГО лота этот ID дописывается в подробное описание "
                "лота на FunPay АВТОМАТИЧЕСКИ через API, сразу после привязки тега — "
                "ничего нажимать не нужно.\n"
                "💡 Если лот был добавлен ДО этого обновления — откройте его в меню лота "
                "и нажмите кнопку «🏷 Авто ID», плагин создаст и допишет ID сам.\n"
                "FunPay копирует описание лота в описание заказа при покупке, поэтому "
                "ID будет виден и в заказе.\n"
                "Когда приходит заказ, плагин просматривает все #теги в его описании по "
                "порядку: тег, совпадающий с номером самого заказа (например "
                "<code>#41234567</code>), всегда молча игнорируется — это не ID лота, а "
                "просто подставленный FunPay номер. Как только среди оставшихся тегов "
                "находится ID, привязанный к одному из ваших лотов — плагин однозначно "
                "определяет этот лот и уже по его настройкам (обычному тегу) подбирает "
                "свободный аккаунт для выдачи.\n"
                "⚠️ Если ID лота не найден в подробном описании — плагин покажет "
                "предупреждение в карточке лота.\n\n"
                "<b>Количество товара в лоте = количество часов.</b>\n"
                "Покупатель покупает 3 единицы → получает 3 часа аренды.\n\n"
                "<b>Лот-продление:</b>\n"
                "Привяжите отдельный лот для продления — покупатель напишет "
                "<code>!продлить</code> и получит ссылку на него.\n\n"
                "📢 Вопросы: <a href=\"https://t.me/DzhantDev\">@DzhantDev</a>"
            )
            kb = K(row_width=1)
            kb.add(B("⬅️ Назад", None, CBT.ABOUT))
            edit(c.message, howto_text, kb)
            return

        text = (
            f"<b>⚡ ASR+ — Автоматическая аренда Steam</b>\n\n"
            f"∟ Версия: <code>{VERSION}</code>\n"
            f"∟ Разработчик: <a href=\"https://t.me/DzhantDev\">@DzhantDev</a>\n\n"
            f"📢 Канал: <a href=\"https://t.me/DzhantDev\">t.me/DzhantDev</a>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>© ASR+ by @DzhantDev</i>"
        )
        kb = K(row_width=1)
        kb.add(B("📖 Инструкция по лотам", None, f"{CBT.ABOUT}:howto"))
        kb.add(B("⬅️ Назад", None, CBT.MAIN))
        edit(c.message, text, kb)


    def cmd_free(m):
        """Команда /free — список свободных аккаунтов"""
        free = [a for a in ACCOUNTS if a.status == RentStatus.FREE]
        if not free:
            bot.send_message(m.chat.id, "🟢 <b>Свободных аккаунтов нет</b>", parse_mode="HTML")
            return
        by_tag = {}
        for a in free:
            by_tag.setdefault(_ntag(a.tag), []).append(a.login)
        lines = []
        for tag, logins in sorted(by_tag.items()):
            lines.append(f"<b>[{tag}]</b> — {len(logins)} шт.\n" + "\n".join(f"  ∟ <code>{l}</code>" for l in logins))
        text = f"🟢 <b>Свободные аккаунты: {len(free)}</b>\n\n" + "\n\n".join(lines)
        bot.send_message(m.chat.id, text[:4000], parse_mode="HTML")

    def cmd_busy(m):
        """Команда /busy — список занятых аккаунтов"""
        active = [a for a in ACCOUNTS if a.status == RentStatus.ACTIVE]
        if not active:
            bot.send_message(m.chat.id, "👤 <b>Активных аренд нет</b>", parse_mode="HTML")
            return
        lines = []
        for a in active:
            order = ORDERS.get(a.current_order) if a.current_order else None
            remaining = _remaining_str(a.rental_end) if a.rental_end else "—"
            buyer = order.buyer if order else (a.owner or "—")
            hours = f"{order.hours}ч" if order else "—"
            lines.append(
                f"∟ <b>{a.login}</b> [{a.tag}]\n"
                f"   👤 {buyer} | ⏱ осталось: <b>{remaining}</b> | 📦 {hours}"
            )
        text = f"👤 <b>Занятые аккаунты: {len(active)}</b>\n\n" + "\n\n".join(lines)
        bot.send_message(m.chat.id, text[:4000], parse_mode="HTML")

    def open_config(c):
        edit(c.message, "⚙️ <b>Настройки авто-лотов и автоматики</b>\n\nЗдесь можно включить/выключить автоматическое управление лотами.", _config_kb())

    def open_functions(c):
        """Меню Функции — конфиг, сообщения, уведомления, история, ЧС, массовые аккаунты"""
        kb = K(row_width=1)
        kb.add(B("⚙️ Конфиг", None, CBT.CONFIG))
        kb.add(B("💬 Сообщения", None, CBT.MSGS))
        kb.add(B("🔔 Уведомления", None, CBT.NOTIFS))
        kb.add(B("📜 История", None, f"{CBT.HIST}:1"))
        kb.add(B("🚫 Чёрный список", None, CBT.BLACKLIST))
        kb.add(B("📦 Массовая загрузка аккаунтов", None, CBT.BULK_ACCS))
        kb.add(B("💾 Бэкапы паролей", None, CBT.PWD_BACKUPS))
        kb.add(B("⬅️ Назад", None, CBT.MAIN))
        bl_count = len(SETTINGS.blacklist)
        total_accs = len(ACCOUNTS)
        edit(c.message,
             f"<b>🛠 Функции</b>\n\n"
             f"∟ Заблокированных пользователей: <code>{bl_count}</code>\n"
             f"∟ Аккаунтов в базе: <code>{total_accs}</code>",
             kb)

    def open_pwd_backups(c):
        """Список аккаунтов с сохранёнными (бэкап) паролями."""
        with _pwd_backup_lock:
            items = sorted(
                ((k, v) for k, v in PWD_BACKUPS.items() if v.get("bot") or v.get("human")),
                key=lambda kv: (kv[1].get("login") or "").lower()
            )
        kb = K(row_width=1)
        if not items:
            edit(c.message,
                 "💾 <b>Бэкапы паролей</b>\n\nПока нет сохранённых паролей.\n\n"
                 "∟ Сюда попадают пароли, которые менял <b>бот</b> (хранятся последние "
                 f"{PWD_BACKUP_LIMIT} шт.), и пароли, заданные <b>вручную</b> (хранятся последние "
                 f"{PWD_BACKUP_HUMAN_LIMIT} шт.).",
                 _back_kb(CBT.FUNCTIONS))
            return
        for acc_id, data in items:
            login = data.get("login") or f"#{acc_id}"
            bcount = len(data.get("bot", []))
            hcount = len(data.get("human", []))
            kb.add(B(f"🔑 {login}  (бот: {bcount} / ручн: {hcount})", None,
                     f"{CBT.PWD_BACKUP_ACC}:{acc_id}"))
        kb.add(B("📄 Скачать все (.txt)", None, CBT.PWD_BACKUP_DL_ALL))
        kb.add(B("⬅️ Назад", None, CBT.FUNCTIONS))
        edit(c.message,
             "💾 <b>Бэкапы паролей</b>\n\n"
             "∟ <i>бот</i> — пароли, изменённые автоматически/ботом (хранятся последние "
             f"{PWD_BACKUP_LIMIT} шт., старые удаляются)\n"
             "∟ <i>ручн</i> — пароли, заданные администратором вручную (хранятся последние "
             f"{PWD_BACKUP_HUMAN_LIMIT} шт., старые удаляются)\n\n"
             "Выберите аккаунт:",
             kb)

    def open_pwd_backup_detail(c):
        acc_id = _pid(c)
        data = PWD_BACKUPS.get(str(acc_id))
        if not data:
            return answer(c, "❌ Нет данных", True)
        login = data.get("login") or f"#{acc_id}"
        bot_list = list(reversed(data.get("bot", [])))
        human_list = list(reversed(data.get("human", [])))
        lines = [f"💾 <b>Бэкап паролей: <code>{login}</code></b>\n"]
        lines.append(f"🤖 <b>Изменено ботом</b> ({len(bot_list)}/{PWD_BACKUP_LIMIT}):")
        if bot_list:
            for r in bot_list:
                lines.append(f"  ∟ <code>{r.get('password','')}</code>  —  {r.get('changed_at','')}")
        else:
            lines.append("  ∟ (пусто)")
        lines.append("")
        lines.append(f"✏️ <b>Изменено вручную</b> ({len(human_list)}/{PWD_BACKUP_HUMAN_LIMIT}):")
        if human_list:
            for r in human_list:
                lines.append(f"  ∟ <code>{r.get('password','')}</code>  —  {r.get('changed_at','')}")
        else:
            lines.append("  ∟ (пусто)")
        kb = K(row_width=1)
        kb.add(B("📄 Скачать .txt", None, f"{CBT.PWD_BACKUP_DL_ACC}:{acc_id}"))
        kb.add(B("⬅️ Назад", None, CBT.PWD_BACKUPS))
        edit(c.message, "\n".join(lines)[:4000], kb)

    def _pwd_backup_txt_block(acc_id, data) -> str:
        login = data.get("login") or f"#{acc_id}"
        bot_list = data.get("bot", [])
        human_list = data.get("human", [])
        lines = [
            f"  #{acc_id}  {login}",
            f"  {'─' * 36}",
            f"  [БОТ — последние {len(bot_list)}/{PWD_BACKUP_LIMIT}]",
        ]
        if bot_list:
            for r in reversed(bot_list):
                lines.append(f"    {r.get('changed_at','—')}  :  {r.get('password','')}")
        else:
            lines.append("    (пусто)")
        lines.append(f"  [ВРУЧНУЮ — последние {len(human_list)}/{PWD_BACKUP_HUMAN_LIMIT}]")
        if human_list:
            for r in reversed(human_list):
                lines.append(f"    {r.get('changed_at','—')}  :  {r.get('password','')}")
        else:
            lines.append("    (пусто)")
        return "\n".join(lines)

    def pwd_backup_download_all(c):
        answer(c)
        chat_id = c.message.chat.id
        now_str = _fmt(_now())
        with _pwd_backup_lock:
            bk_snapshot = {k: v for k, v in PWD_BACKUPS.items() if v.get("bot") or v.get("human")}
        if not bk_snapshot:
            return bot.send_message(chat_id, "❌ Нет сохранённых паролей для экспорта", parse_mode="HTML")
        lines = [
            "ASRplus — БЭКАПЫ ПАРОЛЕЙ (все аккаунты)",
            f"Экспорт: {now_str}",
            f"Аккаунтов: {len(bk_snapshot)} шт.",
            "=" * 40,
        ]
        for acc_id, data in sorted(bk_snapshot.items(), key=lambda kv: (kv[1].get("login") or "").lower()):
            lines.append("")
            lines.append(_pwd_backup_txt_block(acc_id, data))
        lines += ["", "=" * 40]
        _send_txt_file(
            chat_id, "pwd_backups.txt", "\n".join(lines),
            f"💾 <b>pwd_backups.txt</b>\n∟ Бэкапы паролей\n∟ {len(bk_snapshot)} аккаунтов  |  {now_str}"
        )

    def pwd_backup_download_acc(c):
        acc_id = _pid(c)
        answer(c)
        chat_id = c.message.chat.id
        now_str = _fmt(_now())
        data = PWD_BACKUPS.get(str(acc_id))
        if not data:
            return bot.send_message(chat_id, "❌ Нет данных для этого аккаунта", parse_mode="HTML")
        login = data.get("login") or f"#{acc_id}"
        lines = [
            f"ASRplus — БЭКАП ПАРОЛЕЙ: {login}",
            f"Экспорт: {now_str}",
            "=" * 40,
            "",
            _pwd_backup_txt_block(acc_id, data),
            "",
            "=" * 40,
        ]
        _send_txt_file(
            chat_id, f"pwd_backup_{login}.txt", "\n".join(lines),
            f"💾 <b>pwd_backup_{login}.txt</b>\n∟ Бэкап паролей: <code>{login}</code>  |  {now_str}"
        )

    def open_blacklist(c):
        """Меню чёрного списка"""
        bl = SETTINGS.blacklist
        kb = K(row_width=1)
        for uname in bl:
            kb.add(B(f"🚫 {uname}  ❌", None, f"{CBT.BLACKLIST_DEL}:{uname}"))
        kb.add(B("➕ Добавить в ЧС", None, CBT.BLACKLIST_ADD))
        kb.add(B("⬅️ Назад", None, CBT.FUNCTIONS))
        txt = f"<b>🚫 Чёрный список</b> ({len(bl)} польз.)\n\n"
        if bl:
            txt += "\n".join(f"∟ <code>{u}</code>" for u in bl)
            txt += "\n\nНажмите на пользователя чтобы удалить."
        else:
            txt += "Список пуст."
        edit(c.message, txt, kb)

    def blacklist_add_start(c):
        answer(c)
        _temp_storage.setdefault(c.from_user.id, {})
        _ask(c.message.chat.id, c.from_user.id, States.BLACKLIST_ADD,
             "🚫 <b>Добавить в чёрный список</b>\n\nВведите <b>ник покупателя</b> (как на FunPay):",
             _back_kb(CBT.BLACKLIST))

    def _h_blacklist_add(m):
        uname = (m.text or "").strip()
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        if not uname:
            send(m.chat.id, "❌ Ник не может быть пустым", _back_kb(CBT.BLACKLIST))
            return
        SETTINGS.add_to_blacklist(uname)
        send(m.chat.id, f"✅ <code>{uname}</code> добавлен в чёрный список", _back_kb(CBT.BLACKLIST))

    def blacklist_del(c):
        uname = c.data.split(":", 1)[1] if ":" in c.data else ""
        if not uname:
            return answer(c, "❌ Ошибка", True)
        SETTINGS.remove_from_blacklist(uname)
        answer(c, f"✅ {uname} удалён из ЧС")
        open_blacklist(c)

    def acc_set_limit_start(c):
        acc = AccountRepo.get(_pid(c))
        if not acc:
            return answer(c, "❌ Не найден", True)
        answer(c)
        _temp_storage.setdefault(c.from_user.id, {})["lim_acc_id"] = acc.id
        cur = f"{acc.time_limit_hours}ч" if acc.time_limit_hours else "не задан"
        _ask(c.message.chat.id, c.from_user.id, States.SET_LIMIT,
             f"⏱ <b>Лимит времени аренды</b>\n"
             f"∟ Аккаунт: <code>{acc.login}</code>\n"
             f"∟ Текущий лимит: <code>{cur}</code>\n\n"
             f"Введите максимальное кол-во часов аренды (или <code>0</code> чтобы убрать лимит):",
             _back_kb(f"{CBT.ACC_DETAIL}:{acc.id}"))

    def _h_set_limit(m):
        d = _temp_storage.get(m.from_user.id, {})
        aid = d.get("lim_acc_id")
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        if not aid:
            send(m.chat.id, "❌ Данные утеряны", _main_kb())
            return
        try:
            val = float((m.text or "").strip())
            if val < 0:
                raise ValueError
        except ValueError:
            send(m.chat.id, "❌ Введите положительное число или 0", _back_kb(f"{CBT.ACC_DETAIL}:{aid}"))
            return
        with _data_lock:
            acc = AccountRepo.get(aid)
            if not acc:
                send(m.chat.id, "❌ Аккаунт не найден", _main_kb())
                return
            acc.time_limit_hours = val if val > 0 else None
            _save_accounts()
        if val > 0:
            send(m.chat.id, f"✅ Лимит для <code>{acc.login}</code> установлен: <code>{val}ч</code>",
                 _back_kb(f"{CBT.ACC_DETAIL}:{aid}"))
        else:
            send(m.chat.id, f"✅ Лимит для <code>{acc.login}</code> снят",
                 _back_kb(f"{CBT.ACC_DETAIL}:{aid}"))

    def toggle_setting(c):
        p = _p(c)
        if p not in ("enabled", "autoback_on_error", "auto_disable_lots", "auto_enable_lots",
                     "auto_free_on_error", "save_deleted_acc",
                     "notification_order_completed", "notification_error", "notification_refund",
                     "notification_preparing"):
            return answer(c, "❌ Недопустимое поле", True)
        SETTINGS.toggle(p)
        if p.startswith("notification"):
            open_notifs(c)
        elif p in ("auto_disable_lots", "auto_enable_lots", "autoback_on_error", "auto_free_on_error", "save_deleted_acc"):
            open_config(c)
        else:
            open_main(c)

    def open_acc_menu(c):
        kb = K(row_width=1)
        kb.add(B("➕ Добавить аккаунт", None, CBT.ACC_ADD))
        if ACCOUNTS:
            kb.row(B("📜 Список аккаунтов", None, f"{CBT.ACC_LIST}:0"))
            kb.row(B("🏷 По тегам", None, f"{CBT.ACC_BY_TAG}:0"))
            kb.add(B("🔍 Поиск аккаунта", None, CBT.ACC_SEARCH))
        kb.add(B("⬅️ Назад", None, CBT.MAIN))
        edit(c.message, "<b>📂 Управление аккаунтами</b>", kb)

    def open_bulk_accs(c):
        answer(c)
        total = len(ACCOUNTS)
        text = (
            f"<b>📦 Массовая загрузка аккаунтов</b>\n\n"
            f"∟ Сейчас в базе: <b>{total}</b> аккаунтов\n\n"
            f"<b>Загрузить:</b> отправьте файл <code>accounts_bulk.json</code>\n"
            f"<i>Формат: список объектов, каждый содержит login, password, tag, mafile</i>\n\n"
            f"<b>Выгрузить:</b> бот отправит файл который можно загрузить обратно"
        )
        kb = K(row_width=1)
        kb.add(B("⬆️ Загрузить аккаунты (JSON)", None, CBT.BULK_UPLOAD))
        kb.add(B("⬇️ Выгрузить аккаунты (JSON)", None, CBT.BULK_DOWNLOAD))
        kb.add(B("⬅️ Назад", None, CBT.ACC_MENU))
        edit(c.message, text, kb)

    def bulk_upload_start(c):
        answer(c)
        _temp_storage.setdefault(c.from_user.id, {})
        _ask(
            c.message.chat.id, c.from_user.id, States.BULK_MAFILE,
            "📤 <b>Загрузка аккаунтов</b>\n\n"
            "Отправьте файл <code>accounts_bulk.json</code>\n\n"
            "Формат файла:\n"
            "<pre>[\n"
            "  {\n"
            '    "login": "username",\n'
            '    "password": "pass123",\n'
            '    "tag": "default",\n'
            '    "mafile": { ... }\n'
            "  },\n"
            "  ...\n"
            "]</pre>\n\n"
            "⚠️ Поле <code>mafile</code> должно содержать полный JSON содержимый .maFile",
            _back_kb(CBT.BULK_ACCS)
        )

    def _h_bulk_mafile(m):
        if not tg.check_state(m.chat.id, m.from_user.id, States.BULK_MAFILE):
            return
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        content = None
        # Пробуем читать файл
        if m.document:
            try:
                fi = bot.get_file(m.document.file_id)
                raw = bot.download_file(fi.file_path)
                content = raw.decode("utf-8")
            except Exception as e:
                send(m.chat.id, f"❌ Ошибка чтения файла: {_safe_err(e)}", _back_kb(CBT.BULK_ACCS))
                return
        elif m.text:
            content = m.text.strip()

        if not content:
            send(m.chat.id, "❌ Отправьте файл accounts_bulk.json или JSON текст", _back_kb(CBT.BULK_ACCS))
            return

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            send(m.chat.id, f"❌ Невалидный JSON: {_safe_err(e)}", _back_kb(CBT.BULK_ACCS))
            return

        if not isinstance(data, list):
            send(m.chat.id, "❌ Ожидается список (массив) аккаунтов", _back_kb(CBT.BULK_ACCS))
            return

        if not data:
            send(m.chat.id, "❌ Файл пустой (нет аккаунтов)", _back_kb(CBT.BULK_ACCS))
            return

        # Валидируем каждый аккаунт
        errors = []
        valid = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                errors.append(f"[{i+1}] не словарь")
                continue
            login = (item.get("login") or "").strip()
            password = (item.get("password") or "").strip()
            tag = _ntag(item.get("tag") or "default")
            mafile = item.get("mafile")

            if not login:
                errors.append(f"[{i+1}] нет поля login")
                continue
            if not password:
                errors.append(f"[{i+1}] {login}: нет поля password")
                continue
            if not isinstance(mafile, dict):
                errors.append(f"[{i+1}] {login}: mafile должен быть объектом")
                continue
            missing = _validate_mafile(mafile)
            if missing:
                errors.append(f"[{i+1}] {login}: в mafile нет {', '.join(missing)}")
                continue
            valid.append({"login": login, "password": password, "tag": tag, "mafile": mafile})

        if not valid:
            err_lines = "\n".join(errors[:15])
            send(m.chat.id, f"❌ Нет валидных аккаунтов.\n\nОшибки:\n{err_lines}", _back_kb(CBT.BULK_ACCS))
            return

        # Сохраняем валидные данные в temp_storage и показываем подтверждение
        _temp_storage.setdefault(m.from_user.id, {})["bulk_data"] = valid
        _temp_storage[m.from_user.id]["bulk_errors"] = errors

        skip_existing = sum(1 for v in valid if any(a.login.lower() == v["login"].lower() for a in ACCOUNTS))
        to_add = len(valid) - skip_existing

        warn_text = ""
        if errors:
            warn_text = f"\n⚠️ Пропущено с ошибками: {len(errors)}"
        if skip_existing:
            warn_text += f"\n⚠️ Уже существует (будут пропущены): {skip_existing}"

        kb = K(row_width=2)
        kb.add(B("✅ Добавить", None, f"{CBT.BULK_CONFIRM}:yes"),
               B("❌ Отмена", None, CBT.BULK_ACCS))

        send(m.chat.id,
             f"📦 <b>Подтверждение загрузки</b>\n\n"
             f"∟ Всего в файле: <b>{len(data)}</b>\n"
             f"∟ Будет добавлено: <b>{to_add}</b>{warn_text}\n\n"
             f"Продолжить?",
             kb)

    def bulk_confirm(c):
        action = _p(c)
        answer(c)
        if action != "yes":
            open_bulk_accs(c)
            return
        data = _temp_storage.get(c.from_user.id, {}).get("bulk_data", [])
        if not data:
            return answer(c, "❌ Данные утеряны", True)

        added = []
        skipped = []
        errors_add = []
        for item in data:
            login = item["login"]
            password = item["password"]
            tag = item["tag"]
            mafile = item["mafile"]
            # Используем login из mafile если есть
            mafile_login = (mafile.get("account_name") or "").strip()
            actual_login = mafile_login if mafile_login else login
            ok, msg = AccountRepo.add(actual_login, password, mafile, tag)
            if ok:
                added.append(actual_login)
            else:
                if "уже существует" in msg:
                    skipped.append(actual_login)
                else:
                    errors_add.append(f"{actual_login}: {msg}")

        _save_accounts()
        _invalidate_lots_cache()

        # Авто-включение лотов если включено
        if added and SETTINGS.auto_enable_lots and cardinal_ref:
            tags_to_enable = list({_ntag(item["tag"]) for item in data if (item.get("login") or "").strip() in added or
                                   (item.get("mafile", {}).get("account_name") or "").strip() in added})
            def _auto_enable_bulk(tags=tags_to_enable):
                for tag in tags:
                    toggled = _toggle_fp_lots_for_tag(cardinal_ref, tag, True)
                    if toggled and tg_logs:
                        tg_logs.lots_auto_enabled(tag, toggled)
            threading.Thread(target=_auto_enable_bulk, daemon=True).start()

        result_lines = [f"✅ <b>Загрузка завершена</b>\n",
                        f"∟ Добавлено: <b>{len(added)}</b>"]
        if skipped:
            result_lines.append(f"∟ Пропущено (уже есть): <b>{len(skipped)}</b>")
        if errors_add:
            result_lines.append(f"∟ Ошибки: <b>{len(errors_add)}</b>")
            result_lines.append("\nОшибки:")
            result_lines.extend(f"  ∟ {e}" for e in errors_add[:10])
        if added:
            result_lines.append(f"\nДобавленные ({min(len(added),10)} из {len(added)}):")
            result_lines.extend(f"  ∟ <code>{l}</code>" for l in added[:10])
            if len(added) > 10:
                result_lines.append(f"  ... и ещё {len(added)-10}")

        kb = K(row_width=1)
        kb.add(B("⬅️ Назад", None, CBT.ACC_MENU))
        edit(c.message, "\n".join(result_lines), kb)

    def bulk_download(c):
        answer(c)
        chat_id = c.message.chat.id
        with _data_lock:
            accs_snapshot = list(ACCOUNTS)

        if not accs_snapshot:
            return answer(c, "❌ Нет аккаунтов для выгрузки", True)

        export_data = []
        for a in accs_snapshot:
            export_data.append({
                "login": a.login,
                "password": a.password,
                "tag": a.tag,
                "mafile": a.mafile
            })

        content = json.dumps(export_data, indent=2, ensure_ascii=False, default=str)
        data_bytes = content.encode("utf-8")
        now_str = _fmt(_now()).replace(":", "-").replace(" ", "_")
        filename = f"accounts_bulk_{now_str}.json"

        try:
            bot.send_document(
                chat_id,
                (filename, data_bytes),
                caption=(
                    f"📦 <b>{filename}</b>\n"
                    f"∟ Аккаунтов: {len(export_data)}\n"
                    f"∟ Содержит: login, password, tag, mafile\n"
                    f"∟ Для загрузки: Аккаунты → Массовая загрузка → Загрузить"
                ),
                parse_mode="HTML"
            )
            logger.info(f"[ASRplus] Выгружено {len(export_data)} аккаунтов в {filename}")
        except Exception as e:
            logger.warning(f"[ASRplus] Ошибка выгрузки аккаунтов: {e}")
            bot.send_message(chat_id, f"❌ Не удалось отправить файл: {_safe_err(e)}", parse_mode="HTML")

    def open_acc_list(c):
        pg = _pid(c)
        kb = K(row_width=1)
        total = len(ACCOUNTS)
        tp = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        pg = max(0, min(pg, tp - 1))
        start, end = pg * PAGE_SIZE, (pg + 1) * PAGE_SIZE
        for acc in ACCOUNTS[start:end]:
            icon = ICON_STATUS.get(acc.status, "❓")
            owner = f' | {acc.owner}' if acc.owner else ''
            kb.add(B(f"{icon} {acc.login} [{acc.tag}]{owner}", None, f"{CBT.ACC_DETAIL}:{acc.id}"))
        nav = []
        if pg > 0:
            nav.append(B("⬅️", None, f"{CBT.ACC_LIST}:{pg - 1}"))
        nav.append(B(f"{pg + 1}/{tp}", None, _CBT.EMPTY))
        if end < total:
            nav.append(B("➡️", None, f"{CBT.ACC_LIST}:{pg + 1}"))
        if nav:
            kb.row(*nav)
        kb.add(B("⬅️ Назад", None, CBT.ACC_MENU))
        edit(c.message, f"<b>📜 Аккаунты ({total})</b>", kb)

    def open_acc_by_tag(c):
        # Выбор тега для просмотра аккаунтов
        parts = c.data.split(':')
        selected_tag = parts[1] if len(parts) > 1 and parts[1] else None
        tags = AccountRepo.all_tags()
        if not tags:
            return answer(c, '❌ Нет аккаунтов', True)
        if not selected_tag or selected_tag == '0':
            # Показываем список тегов
            kb = K(row_width=2)
            for tag in sorted(tags):
                free = AccountRepo.count_free(tag).get(_ntag(tag), 0)
                total_tag = sum(1 for a in ACCOUNTS if _ntag(a.tag) == _ntag(tag))
                free_icon = "🟢" if free > 0 else "🔴"
                kb.add(B(f'🏷 {tag}  ({total_tag} акк / {free} {free_icon})', None, f'{CBT.ACC_BY_TAG}:{tag}'))
            kb.add(B('⬅️ Назад', None, CBT.ACC_MENU))
            edit(c.message, '<b>🏷 Сортировка по тегам</b>\n\nВыберите тег:', kb)
        else:
            # Показываем аккаунты выбранного тега
            accs = [a for a in ACCOUNTS if _ntag(a.tag) == _ntag(selected_tag)]
            pg = int(parts[2]) if len(parts) > 2 else 0
            total = len(accs)
            tp = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            pg = max(0, min(pg, tp - 1))
            start, end = pg * PAGE_SIZE, (pg + 1) * PAGE_SIZE
            kb = K(row_width=1)
            for acc in accs[start:end]:
                icon = ICON_STATUS.get(acc.status, '❓')
                owner = f' | {acc.owner}' if acc.owner else ''
                kb.add(B(f'{icon} {acc.login}{owner}', None, f'{CBT.ACC_DETAIL}:{acc.id}'))
            nav = []
            if pg > 0:
                nav.append(B('⬅️', None, f'{CBT.ACC_BY_TAG}:{selected_tag}:{pg-1}'))
            nav.append(B(f'{pg+1}/{tp}', None, _CBT.EMPTY))
            if end < total:
                nav.append(B('➡️', None, f'{CBT.ACC_BY_TAG}:{selected_tag}:{pg+1}'))
            if nav:
                kb.row(*nav)
            kb.add(B('🔙 К тегам', None, f'{CBT.ACC_BY_TAG}:0'))
            edit(c.message, f'<b>🏷 Тег: {selected_tag} ({total})</b>', kb)


    def acc_search_start(c):
        answer(c)
        _temp_storage.setdefault(c.from_user.id, {})
        _ask(c.message.chat.id, c.from_user.id, States.ACC_SEARCH,
             "🔍 <b>Поиск аккаунта</b>\n\nВведите логин (или часть):", _back_kb(CBT.ACC_MENU))

    def _h_acc_search(m):
        query = (m.text or "").strip().lower()
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        if not query:
            send(m.chat.id, "❌ Введите логин для поиска", _back_kb(CBT.ACC_MENU))
            return
        results = [a for a in ACCOUNTS if query in a.login.lower() or query in _ntag(a.tag)]
        if not results:
            send(m.chat.id, f"🔍 <b>Ничего не найдено</b> по запросу: <code>{query}</code>", _back_kb(CBT.ACC_MENU))
            return
        kb = K(row_width=1)
        for acc in results[:20]:
            icon = ICON_STATUS.get(acc.status, "❓")
            owner = f" | {acc.owner}" if acc.owner else ""
            kb.add(B(f"{icon} {acc.login} [{acc.tag}]{owner}", None, f"{CBT.ACC_DETAIL}:{acc.id}"))
        kb.add(B("⬅️ Назад", None, CBT.ACC_MENU))
        send(m.chat.id, f"🔍 <b>Результаты ({len(results)})</b> по: <code>{query}</code>", kb)

    _STATUS_LABEL = {
        RentStatus.FREE: "Свободен",
        RentStatus.ACTIVE: "В аренде",
        RentStatus.BUSY: "Занят",
        RentStatus.ERROR: "Ошибка",
        RentStatus.FINISHED: "Завершён",
        RentStatus.REFUND: "Возврат",
    }

    def _acc_text(acc):
        icon = ICON_STATUS.get(acc.status, "❓")
        status_lbl = _STATUS_LABEL.get(acc.status, acc.status)
        lines = [
            f"{icon} <b>{acc.login}</b>  <code>#{acc.id}</code>",
            "",
            f"∟ 🏷 Тег: <code>{acc.tag}</code>",
            f"∟ 📶 Статус: <b>{status_lbl}</b>",
            f"∟ 🔐 Пароль: <code>{acc.password}</code>",
        ]
        if acc.status == RentStatus.ACTIVE:
            if acc.owner:
                lines.append(f"∟ 👤 Арендатор: <code>{acc.owner}</code>")
            if acc.rental_start:
                lines.append(f"∟ 🟢 Начало: <code>{acc.rental_start}</code>")
            if acc.rental_end:
                lines.append(f"∟ 🔴 Конец: <code>{acc.rental_end}</code>")
                lines.append(f"∟ ⏳ Осталось: <b>{_remaining_str(acc.rental_end)}</b>")
            if acc.current_order:
                lines.append(f"∟ 🧾 Заказ: <code>{acc.current_order[:20]}...</code>")
        lines.append(f"∟ 🔢 Доступов: <code>{acc.access_count}</code>")
        if acc.time_limit_hours:
            lines.append(f"∟ ⏱ Лимит аренды: <code>{acc.time_limit_hours}ч</code>")
        if acc.status == RentStatus.ERROR:
            lines.append("")
            lines.append("⚠️ <i>Требуется внимание: аккаунт помечен как неисправный</i>")
        return "\n".join(lines)

    def _acc_kb(acc):
        kb = K(row_width=2)
        kb.add(B("🔑 Выдать код", None, f"{CBT.ACC_CODE}:{acc.id}"),
               B("🔄 Сменить пароль", None, f"{CBT.ACC_CHPWD}:{acc.id}"))
        kb.add(B("✏️ Обновить пароль", None, f"{CBT.ACC_SET_PWD}:{acc.id}"),
               B("🗂 Обновить maFile", None, f"{CBT.ACC_EDIT_MAFILE}:{acc.id}"))
        if acc.status in (RentStatus.ACTIVE, RentStatus.BUSY):
            kb.add(B("⏹ Остановить", None, f"{CBT.ACC_STOP}:{acc.id}"),
                   B("⏰ Продлить", None, f"{CBT.ACC_EXTEND}:{acc.id}"))
        if acc.status in (RentStatus.FREE, RentStatus.ERROR):
            kb.add(B("🤝 Ручная аренда", None, f"{CBT.ACC_MANUAL}:{acc.id}"))
        if acc.status == RentStatus.ERROR:
            kb.add(B("🔓 Сброс FREE", None, f"{CBT.ACC_RESET}:{acc.id}"))
        kb.add(B("⏱ Установить лимит", None, f"{CBT.ACC_SET_LIMIT}:{acc.id}"))
        kb.add(B("🗑 Удалить", None, f"{CBT.ACC_DEL_CONFIRM}:{acc.id}"))
        kb.add(B("⬅️ К списку", None, f"{CBT.ACC_LIST}:0"))
        return kb

    def open_acc_detail(c):
        acc = AccountRepo.get(_pid(c))
        if not acc:
            return answer(c, "❌ Не найден", True)
        edit(c.message, _acc_text(acc), _acc_kb(acc))

    def acc_del_confirm(c):
        aid = _pid(c)
        acc = AccountRepo.get(aid)
        if not acc:
            return answer(c, "❌ Не найден", True)
        if acc.status == RentStatus.ACTIVE:
            return answer(c, "❌ Аккаунт сейчас в аренде!", True)
        text = (f"⚠️ <b>Удалить аккаунт?</b>\n\n∟ Логин: <code>{acc.login}</code>\n"
                f"∟ Тег: <code>{acc.tag}</code>\n∟ Статус: <code>{acc.status}</code>\n\n❗ Это действие необратимо!")
        kb = K(row_width=2)
        kb.add(B("✅ Да", None, f"{CBT.ACC_DEL_YES}:{aid}"), B("❌ Нет", None, f"{CBT.ACC_DEL_NO}:{aid}"))
        edit(c.message, text, kb)

    def acc_del_yes(c):
        aid = _pid(c)
        acc = AccountRepo.get(aid)
        login = acc.login if acc else str(aid)
        # Сохраняем данные перед удалением если включена настройка
        if acc and SETTINGS.save_deleted_acc:
            try:
                backup_text = (
                    f"🗑 <b>Аккаунт удалён — резервная копия</b>\n\n"
                    f"∟ Логин: <code>{acc.login}</code>\n"
                    f"∟ Пароль: <code>{acc.password}</code>\n"
                    f"∟ Тег: <code>{acc.tag}</code>\n"
                    f"∟ Статус: <code>{acc.status}</code>\n"
                    f"∟ Удалён: <code>{_fmt(_now())}</code>"
                )
                bot.send_message(c.message.chat.id, backup_text, parse_mode="HTML")
            except Exception as _be:
                logger.warning(f"[ASRplus] Ошибка отправки резервной копии: {_be}")
        AccountRepo.delete(aid)
        answer(c, f"✅ {login} удалён")
        c.data = f"{CBT.ACC_LIST}:0"
        open_acc_list(c)

    def acc_del_no(c):
        aid = _pid(c)
        answer(c, "❌ Удаление отменено")
        c.data = f"{CBT.ACC_DETAIL}:{aid}"
        open_acc_detail(c)

    def acc_code(c):
        acc = AccountRepo.get(_pid(c))
        if not acc:
            return answer(c, "❌ Не найден", True)
        ss = acc.mafile.get("shared_secret", "")
        if not ss:
            return answer(c, "❌ Нет shared_secret", True)
        code = SteamGuard.code_sync(ss)
        if code in ("ERROR", "NO_SECRET"):
            return answer(c, "❌ Ошибка генерации", True)
        if acc.status == RentStatus.ACTIVE and acc.owner_chat_id:
            end_time_str = acc.rental_end
            if not end_time_str:
                order = ORDERS.get(acc.current_order) if acc.current_order else None
                if order and hasattr(order, 'hours') and order.hours:
                    try:
                        recovered_end = _fmt(_now() + timedelta(hours=float(order.hours)))
                        with _data_lock:
                            acc.rental_end = recovered_end
                            _save_accounts()
                        end_time_str = recovered_end
                        logger.warning(f"[ASRplus] rental_end был None для acc_id={acc.id}, восстановлен: {recovered_end}")
                    except Exception:
                        pass
            _send_fp(card, acc.owner_chat_id,
                     _tmpl(SETTINGS.messages.guard_code, code=code, end_time=end_time_str or "неизвестно"))
        kb = K(row_width=2)
        kb.add(B("🔄 Новый код", None, f"{CBT.ACC_CODE}:{acc.id}"),
               B("⬅️ К аккаунту", None, f"{CBT.ACC_DETAIL}:{acc.id}"))
        edit(c.message, f"🔑 <b>Steam Guard код</b>\n\n∟ Аккаунт: <code>{acc.login}</code>\n"
                        f"∟ Код: <code>{code}</code>\n∟ Действителен ~30 сек", kb)

    def acc_stop(c):
        acc = AccountRepo.get(_pid(c))
        if not acc:
            return answer(c, "❌ Не найден", True)
        if acc.status not in (RentStatus.ACTIVE, RentStatus.BUSY):
            return answer(c, "ℹ️ Не активна", True)
        with _recovering_lock:
            if acc.id in _recovering_accounts:
                return answer(c, "⏳ Уже идёт остановка", True)
            _recovering_accounts.add(acc.id)
        order = ORDERS.get(acc.current_order) if acc.current_order else None
        owner_chat_id = acc.owner_chat_id
        chat_id = c.message.chat.id
        acc_id = acc.id
        def _do():
            try:
                a = AccountRepo.get(acc_id)
                if a:
                    _recover_account(card, a, order, "MANUAL_STOP")
                    if owner_chat_id:
                        _send_fp(card, owner_chat_id, SETTINGS.messages.rent_over)
                    send(chat_id, f"✅ Аренда <code>{a.login}</code> остановлена.")
            except Exception as e:
                send(chat_id, f"❌ Ошибка остановки: {_safe_err(e)}")
            finally:
                with _recovering_lock:
                    _recovering_accounts.discard(acc_id)
        answer(c)
        edit(c.message, f"⏳ Остановка <code>{acc.login}</code>...", _back_kb(f"{CBT.ACC_DETAIL}:{acc.id}"))
        threading.Thread(target=_do, daemon=True).start()

    def acc_chpwd(c):
        acc = AccountRepo.get(_pid(c))
        if not acc:
            return answer(c, "❌ Не найден", True)
        chat_id = c.message.chat.id
        acc_id = acc.id
        def _do():
            try:
                a = AccountRepo.get(acc_id)
                if not a:
                    send(chat_id, "❌ Аккаунт не найден")
                    return
                np = change_password_sync(a.mafile, a.password, a.id)
                AccountRepo.set_password_bot(a.id, np)
                send(chat_id, f"✅ Пароль <code>{a.login}</code> изменён:\n<code>{np}</code>")
            except Exception as e:
                send(chat_id, f"❌ Ошибка: {_safe_err(e)}")
        answer(c)
        edit(c.message, f"⏳ Смена пароля <code>{acc.login}</code>...", _back_kb(f"{CBT.ACC_DETAIL}:{acc.id}"))
        threading.Thread(target=_do, daemon=True).start()

    def acc_extend_menu(c):
        acc = AccountRepo.get(_pid(c))
        if not acc:
            return answer(c, "❌ Не найден", True)
        kb = K(row_width=3)
        for h in [1, 2, 3, 6, 12, 24]:
            kb.add(B(f"+{h}ч", None, f"{CBT.ACC_EXTEND_DO}:{acc.id}:{h}"))
        kb.add(B("⬅️", None, f"{CBT.ACC_DETAIL}:{acc.id}"))
        edit(c.message, f"⏰ Продлить <code>{acc.login}</code> (часы):", kb)

    def acc_extend_do(c):
        try:
            parts = c.data.split(":")
            aid, h = int(parts[1]), int(parts[2])
        except (IndexError, ValueError):
            return answer(c, "❌ Неверные данные", True)
        ne = AccountRepo.extend_rent(aid, h)
        acc = AccountRepo.get(aid)
        if ne:
            if acc and acc.owner_chat_id:
                _send_fp(card, acc.owner_chat_id, _tmpl(SETTINGS.messages.extended, hours=str(h), end_time=ne))
            edit(c.message, f"✅ <code>{acc.login if acc else aid}</code> +{h}ч\n∟ Окончание: <code>{ne}</code>",
                 _back_kb(f"{CBT.ACC_DETAIL}:{aid}"))
        else:
            answer(c, "❌ Не удалось", True)

    def acc_reset(c):
        aid = _pid(c)
        acc = AccountRepo.get(aid)
        if not acc:
            return answer(c, "❌ Не найден", True)
        if acc.status != RentStatus.ERROR:
            return answer(c, "ℹ️ Не в ERROR", True)
        acc_tag = _ntag(acc.tag)
        if acc.current_order:
            order = ORDERS.get(acc.current_order)
            if order and order.status not in (RentStatus.FINISHED, RentStatus.REFUND):
                order.update(status=RentStatus.FINISHED)
        AccountRepo.reset_to_free(aid)
        answer(c, f"✅ {acc.login} → FREE")
        acc = AccountRepo.get(aid)
        edit(c.message, _acc_text(acc), _acc_kb(acc))
        if SETTINGS.auto_enable_lots and cardinal_ref:
            def _auto_enable_reset(tag=acc_tag):
                toggled = _toggle_fp_lots_for_tag(cardinal_ref, tag, True)
                if toggled and tg_logs:
                    tg_logs.lots_auto_enabled(tag, toggled)
            threading.Thread(target=_auto_enable_reset, daemon=True).start()

    def acc_set_pwd(c):
        acc = AccountRepo.get(_pid(c))
        if not acc:
            return answer(c, "❌ Не найден", True)
        _temp_storage.setdefault(c.from_user.id, {})["sp_acc_id"] = acc.id
        answer(c)
        _ask(c.message.chat.id, c.from_user.id, States.SET_PWD,
             f"✏️ Введите новый пароль для <code>{acc.login}</code>:",
             _back_kb(f"{CBT.ACC_DETAIL}:{acc.id}"))

    def _h_set_pwd(m):
        d = _temp_storage.get(m.from_user.id, {})
        aid = d.get("sp_acc_id")
        pwd = (m.text or "").strip()
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        if not aid:
            send(m.chat.id, "❌ Данные утеряны", _main_kb())
            return
        if not pwd:
            send(m.chat.id, "❌ Пароль не может быть пустым", _main_kb())
            return
        ok = AccountRepo.set_password(aid, pwd)
        acc = AccountRepo.get(aid)
        if ok and acc:
            send(m.chat.id, f"✅ Пароль для <code>{acc.login}</code> обновлён", _acc_kb(acc))
        else:
            send(m.chat.id, "❌ Не удалось обновить пароль", _main_kb())

    def acc_edit_mafile(c):
        acc = AccountRepo.get(_pid(c))
        if not acc:
            return answer(c, "❌ Не найден", True)
        _temp_storage.setdefault(c.from_user.id, {})["em_acc_id"] = acc.id
        _temp_storage[c.from_user.id]["em_current_login"] = acc.login
        answer(c)
        _ask(c.message.chat.id, c.from_user.id, States.EDIT_MAFILE,
             f"🗂 Отправьте <b>.maFile</b> для <code>{acc.login}</code> файлом или JSON текстом:",
             _back_kb(f"{CBT.ACC_DETAIL}:{acc.id}"))

    def _read_mafile_content(m):
        if m.content_type == 'document' and m.document:
            file_info = bot.get_file(m.document.file_id)
            file_bytes = bot.download_file(file_info.file_path)
            return file_bytes.decode('utf-8')
        elif m.text:
            return m.text.strip()
        return None

    def _h_mafile_edit(m):
        if not tg.check_state(m.chat.id, m.from_user.id, States.EDIT_MAFILE):
            return
        d = _temp_storage.get(m.from_user.id, {})
        aid = d.get("em_acc_id")
        if not aid:
            _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
            send(m.chat.id, "❌ Данные утеряны", _main_kb())
            return
        try:
            content = _read_mafile_content(m)
        except Exception as e:
            _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
            send(m.chat.id, f"❌ Ошибка чтения: {_safe_err(e)}", _main_kb())
            return
        if content is None:
            _cleanup_dialog(m.chat.id, m.from_user.id, None)
            send(m.chat.id, "❌ Отправьте .maFile файлом или JSON текстом", _main_kb())
            return
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        try:
            mf = json.loads(content)
        except json.JSONDecodeError as e:
            send(m.chat.id, f"❌ Невалидный JSON: {_safe_err(e)}", _main_kb())
            return
        if not isinstance(mf, dict):
            send(m.chat.id, "❌ Неверный формат maFile.", _main_kb())
            return
        missing = _validate_mafile(mf)
        if missing:
            send(m.chat.id, f"❌ Отсутствуют поля: <code>{', '.join(missing)}</code>", _main_kb())
            return
        current_login = d.get("em_current_login", "")
        mafile_login = mf.get("account_name", "").strip()
        ok, err = AccountRepo.set_mafile(aid, mf)
        acc = AccountRepo.get(aid)
        if ok and acc:
            extra = ""
            if mafile_login and current_login and mafile_login.lower() != current_login.lower():
                extra += f"\nℹ️ Логин обновлён: <code>{acc.login}</code>"
            warn = _warn_mafile(mf)
            if warn:
                extra += f"\n⚠️ Нет полей для смены пароля: <code>{', '.join(warn)}</code>"
            send(m.chat.id, f"✅ maFile обновлён{extra}", _acc_kb(acc))
        else:
            send(m.chat.id, f"❌ {err or 'Не удалось обновить maFile'}", _main_kb())

    def acc_manual_start(c):
        acc = AccountRepo.get(_pid(c))
        if not acc:
            return answer(c, "❌ Не найден", True)
        if acc.status not in (RentStatus.FREE, RentStatus.ERROR):
            return answer(c, "ℹ️ Не свободен", True)
        _temp_storage.setdefault(c.from_user.id, {})["man_id"] = acc.id
        answer(c)
        _ask(c.message.chat.id, c.from_user.id, States.MAN_BUYER,
             f"🤝 Ручная аренда <code>{acc.login}</code>\n\nВведите <b>ник покупателя</b>:",
             _back_kb(f"{CBT.ACC_DETAIL}:{acc.id}"))

    def _h_manual_buyer(m):
        _temp_storage.setdefault(m.from_user.id, {})["man_buyer"] = m.text.strip()
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        _ask(m.chat.id, m.from_user.id, States.MAN_HOURS,
             "Введите <b>количество часов</b> аренды (целое число):",
             _back_kb(f"{CBT.ACC_DETAIL}:{_temp_storage.get(m.from_user.id, {}).get('man_id', 0)}"))

    def _h_manual_hours(m):
        d = _temp_storage.get(m.from_user.id, {})
        aid = d.get("man_id")
        buyer = d.get("man_buyer")
        if not aid or not buyer:
            _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
            send(m.chat.id, "❌ Данные утеряны", _main_kb())
            return
        try:
            hours = float(m.text.strip())
            if hours <= 0:
                raise ValueError
        except ValueError:
            _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
            send(m.chat.id, "❌ Введите положительное число часов.", _main_kb())
            return
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        acc = AccountRepo.manual_assign(aid, buyer, hours)
        if acc:
            send(m.chat.id, f"✅ <code>{acc.login}</code> → <code>{acc.owner}</code> на {hours} ч\n"
                            f"∟ Окончание: <code>{acc.rental_end}</code>", _back_kb(f"{CBT.ACC_DETAIL}:{aid}"))
        else:
            send(m.chat.id, "❌ Не удалось (занят?)", _back_kb(f"{CBT.ACC_DETAIL}:{aid}"))

    def start_add(c):
        answer(c)
        _temp_storage[c.from_user.id] = {}
        _ask(c.message.chat.id, c.from_user.id, States.LOGIN, "1️⃣ Введите <b>логин</b>:", _back_kb(CBT.ACC_MENU))

    def _h_login(m):
        if m.text.startswith("/"):
            return
        login = m.text.strip()
        _temp_storage.setdefault(m.from_user.id, {})["login"] = login
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        _ask(m.chat.id, m.from_user.id, States.PASS, "2️⃣ Введите <b>пароль</b>:", _back_kb(CBT.ACC_MENU))

    def _h_pass(m):
        _temp_storage.setdefault(m.from_user.id, {})["password"] = m.text.strip()
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        _ask(m.chat.id, m.from_user.id, States.TAG, "3️⃣ Введите <b>тег</b> (например, default):", _back_kb(CBT.ACC_MENU))

    def _h_tag(m):
        d = _temp_storage.setdefault(m.from_user.id, {})
        d["tag"] = m.text.strip()
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        _ask(m.chat.id, m.from_user.id, States.MAFILE, "4️⃣ Отправьте <b>.maFile</b> (файлом или JSON текстом):",
             _back_kb(CBT.ACC_MENU))

    def _h_mafile(m):
        if not tg.check_state(m.chat.id, m.from_user.id, States.MAFILE):
            return
        try:
            content = _read_mafile_content(m)
        except Exception as e:
            _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
            send(m.chat.id, f"❌ Ошибка чтения: {_safe_err(e)}", _main_kb())
            return
        if content is None:
            _cleanup_dialog(m.chat.id, m.from_user.id, None)
            send(m.chat.id, "❌ Отправьте .maFile файлом или JSON текстом", _main_kb())
            return
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        try:
            mf = json.loads(content)
        except json.JSONDecodeError as e:
            send(m.chat.id, f"❌ Невалидный JSON: {_safe_err(e)}", _main_kb())
            return
        if not isinstance(mf, dict):
            send(m.chat.id, "❌ Неверный формат maFile.", _main_kb())
            return
        missing = _validate_mafile(mf)
        if missing:
            send(m.chat.id, f"❌ Отсутствуют поля: <code>{', '.join(missing)}</code>", _main_kb())
            return
        d = _temp_storage.get(m.from_user.id, {})
        if "login" not in d:
            send(m.chat.id, "❌ Данные потеряны", _main_kb())
            return
        mafile_login = mf.get("account_name", "").strip()
        entered_login = d["login"].strip()
        actual_login = mafile_login if mafile_login else entered_login
        ok, txt = AccountRepo.add(actual_login, d["password"], mf, d["tag"])
        _invalidate_lots_cache()
        if ok:
            extra = ""
            if mafile_login and entered_login.lower() != mafile_login.lower():
                extra += f"\nℹ️ Логин из maFile: <code>{actual_login}</code>"
            warn = _warn_mafile(mf)
            if warn:
                extra += f"\n⚠️ Нет полей для смены пароля: <code>{', '.join(warn)}</code>"
            send(m.chat.id, f"✅ {txt}{extra}", _main_kb())
            if SETTINGS.auto_enable_lots and cardinal_ref:
                acc_tag = _ntag(d["tag"])
                def _auto_enable_add(tag=acc_tag):
                    toggled = _toggle_fp_lots_for_tag(cardinal_ref, tag, True)
                    if toggled and tg_logs:
                        tg_logs.lots_auto_enabled(tag, toggled)
                threading.Thread(target=_auto_enable_add, daemon=True).start()
        else:
            send(m.chat.id, f"❌ {txt}", _main_kb())

    
    def open_lots(c):
        count = len(SETTINGS.lots)
        hourly_count = sum(1 for lid in SETTINGS.lots if (SETTINGS.get_lot(lid) or LotConfig(tag="default")).lot_type != "fixed")
        fixed_count = count - hourly_count
        kb = K(row_width=1)
        kb.add(B(f"⏱ Почасовые лоты ({hourly_count})", None, CBT.LOTS_HOURLY))
        kb.add(B(f"📌 Фиксированные лоты ({fixed_count})", None, CBT.LOTS_FIXED))
        kb.add(B("🏷 Продление по тегам", None, CBT.TAG_EXTEND))
        kb.row(B("🟢 Вкл все", None, CBT.LOTS_ENABLE_ALL), B("🔴 Выкл все", None, CBT.LOTS_DISABLE_ALL))
        kb.add(B("🔄 Обновить", None, CBT.LOTS), B("⬅️ Назад", None, CBT.MAIN))
        text = f"<b>🔗 Лоты</b> — всего: <code>{count}</code>"
        edit(c.message, text, kb)

    def _open_lots_list(c, lot_type: str):
        """Общий рендер списка лотов конкретного типа (почасовые/фиксированные)."""
        ids = [lid for lid in SETTINGS.lots
               if (SETTINGS.get_lot(lid) or LotConfig(tag="default")).lot_type == lot_type]
        kb = K(row_width=1)
        if ids:
            by_tag: Dict[str, list] = {}
            for lid in ids:
                lc = SETTINGS.get_lot(lid)
                tag = lc.tag if lc else "default"
                by_tag.setdefault(tag, []).append(lid)
            for tag in sorted(by_tag.keys()):
                for lid in by_tag[tag]:
                    lc = SETTINGS.get_lot(lid)
                    if lc:
                        free = AccountRepo.count_free(lc.tag).get(_ntag(lc.tag), 0)
                        free_icon = "🟢" if free > 0 else "🔴"
                        display = lc.note if lc.note else f"#{lid}"
                        kb.add(B(f"[{lc.tag}] {display}  ·  {free} {free_icon}", None, f"{CBT.LOT_DETAIL}:{lid}"))
        kb.row(B("➕ Добавить лот", None, f"{CBT.LOT_ADD}:{lot_type}"))
        kb.add(B("⬅️ Назад", None, CBT.LOTS))
        title = "⏱ Почасовые лоты" if lot_type == "hourly" else "📌 Фиксированные лоты"
        subtitle = "1 шт = 1 час аренды" if lot_type == "hourly" else "время задаётся в настройках лота"
        text = f"<b>{title}</b>\n<i>{subtitle}</i>\n\nВсего: <code>{len(ids)}</code>"
        if not ids:
            text += "\nЛоты не добавлены."
        edit(c.message, text, kb)

    def open_lots_hourly(c):
        _open_lots_list(c, "hourly")

    def open_lots_fixed(c):
        _open_lots_list(c, "fixed")

    def open_lot_detail(c):
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        lot_url = FUNPAY_LOT_URL.format(lot_id=lid)
        free_count = AccountRepo.count_free(lc.tag).get(_ntag(lc.tag), 0)
        fp_active = None
        try:
            # Публичный список лотов (get_user().get_lots()) не показывает лоты,
            # у которых сейчас 0 в наличии — из-за этого статус всегда был
            # "Нет данных" для таких лотов. Берём статус напрямую со страницы
            # редактирования лота (тот же способ, что использует переключение
            # лотов), это работает независимо от наличия остатка.
            lf = cardinal_ref.account.get_lot_fields(int(lid))
            if lf is not None and lf.active is not None:
                fp_active = bool(lf.active)
        except Exception:
            # Фоллбэк на кэш публичного профиля, если запрос полей лота не удался
            try:
                fp_lots = _get_cached_lots(cardinal_ref)
                fp_lot = next((l for l in fp_lots if str(l.id) == lid), None)
                if fp_lot and fp_lot.active is not None:
                    fp_active = bool(fp_lot.active)
            except Exception:
                pass
        active_str = "🟢 Включён" if fp_active is True else ("🔴 Выключен" if fp_active is False else "⚪ Нет данных")
        note_str = f"\n∟ 📝 Заметка: <code>{lc.note}</code>" if lc.note else ""
        # Инфо о лот-продлении
        extend_info = ""
        if lc.lot_type == "fixed" and lc.extend_options:
            opt_lines = []
            for o in lc.extend_options:
                o_id = str(o.get("lot_id"))
                line = f"#{o_id} ({_fmt_hours(o.get('hours'))}ч)"
                with _extend_lot_timers_lock:
                    if o_id in _extend_lot_timers:
                        line += " ⏱"
                opt_lines.append(line)
            extend_info = "\n∟ 🔄 Лот-продление: " + ", ".join(opt_lines)
        elif lc.extend_lot_id:
            extend_info = f"\n∟ 🔄 Лот-продление: <code>#{lc.extend_lot_id}</code>"
            # Есть ли активный таймер
            with _extend_lot_timers_lock:
                if lc.extend_lot_id in _extend_lot_timers:
                    extend_info += " ⏱ <i>(ожидает покупки)</i>"
        else:
            # Своего продления не задано — проверяем, есть ли общее по тегу
            tag_single = SETTINGS.get_tag_extend_lot_id(lc.tag)
            tag_opts = SETTINGS.get_tag_extend_options(lc.tag)
            if lc.lot_type == "fixed" and tag_opts:
                opt_str = ", ".join(f"#{o.get('lot_id')} ({_fmt_hours(o.get('hours'))}ч)" for o in tag_opts)
                extend_info = f"\n∟ 🔄 Лот-продление (общее по тегу «{lc.tag}»): {opt_str}"
            elif tag_single:
                extend_info = f"\n∟ 🔄 Лот-продление (общее по тегу «{lc.tag}»): <code>#{tag_single}</code>"
        # Проверяем, есть ли уникальный служебный ID лота в подробном описании
        # на FunPay (именно этот ID однозначно
        # определяет лот в заказе, а не обычный тег аккаунта, который может
        # повторяться у нескольких лотов).
        match_tag_for_check = lc.match_tag
        tag_found_in_desc = None
        try:
            _, _, desc_text_check = _get_lot_detailed_description(cardinal_ref, lid)
            found_tags = {_ntag(m) for m in _TAG_RE.findall(desc_text_check or "")}
            if match_tag_for_check:
                tag_found_in_desc = _ntag(match_tag_for_check) in found_tags
            else:
                tag_found_in_desc = False
        except Exception:
            tag_found_in_desc = None
        if tag_found_in_desc is False:
            shown_tag = match_tag_for_check or "будет создан автоматически"
            warn_str = (
                f"\n\n⚠️ <b>ID лота (<code>#{shown_tag}</code>) не найден в подробном описании лота!</b>\n"
                f"∟ Нажмите кнопку «🏷 Авто ID» ниже — плагин сам создаст и допишет "
                f"уникальный ID в конец подробного описания лота на FunPay. "
                f"Именно по нему плагин однозначно узнаёт заказы этого лота."
            )
        else:
            warn_str = ""
        type_label = "📌 Фиксированный" if lc.lot_type == "fixed" else "⏱ Почасовой"
        fixed_hours_line = f"\n∟ ⏱ Время аренды: <code>{lc.fixed_hours}ч</code>" if lc.lot_type == "fixed" and lc.fixed_hours else \
                            ("\n∟ ⚠️ Время аренды не задано!" if lc.lot_type == "fixed" else "")
        text = (
            f"🔗 <b>Лот #{lid}</b>\n\n"
            f"∟ 🗂 Тип: {type_label}{fixed_hours_line}\n"
            f"∟ 🏷 Тег (пул аккаунтов): <code>{lc.tag}</code>\n"
            f"∟ 🆔 ID (для опознания заказа): <code>#{match_tag_for_check or '—'}</code>\n"
            f"∟ 📦 Свободных аккаунтов: <code>{free_count}</code>\n"
            f"∟ 📶 Статус на FunPay: {active_str}\n"
            f"∟ 🌐 Ссылка: {lot_url}{note_str}{extend_info}{warn_str}"
        )
        kb = K(row_width=2)
        kb.add(B("✏️ Изменить тег", None, f"{CBT.LOT_EDIT}:{lid}"),
               B("🔢 Изменить ID", None, f"{CBT.LOT_RENAME}:{lid}"))
        if tag_found_in_desc is False:
            kb.add(B("🏷 Авто ID (дописать ID)", None, f"{CBT.LOT_AUTO_TAG}:{lid}"))
        note_label = "✏️ Изменить заметку" if lc.note else "📝 Добавить заметку"
        kb.add(B(note_label, None, f"{CBT.LOT_NOTE}:{lid}"))
        if lc.lot_type == "fixed":
            fh_label = f"⏱ Время: {lc.fixed_hours}ч ✏️" if lc.fixed_hours else "⏱ Время (не задано) ⚠️"
            kb.add(B(fh_label, None, f"{CBT.LOT_FIXED_HOURS}:{lid}"))
        # Кнопка лот-продление
        if lc.lot_type == "fixed":
            n_opts = len(lc.extend_options)
            if n_opts:
                opt_label = f"🔄 Лот-продление ({n_opts} вариант(а))"
            elif SETTINGS.get_tag_extend_options(lc.tag):
                opt_label = "🔄 Лот-продление (используется общее по тегу)"
            else:
                opt_label = "🔄 Лот-продление (не задан)"
            kb.add(B(opt_label, None, f"{CBT.LOT_EXTEND_LOT}:{lid}"))
        elif lc.extend_lot_id:
            kb.add(B(f"🔄 Лот-продление: #{lc.extend_lot_id} ✏️", None, f"{CBT.LOT_EXTEND_LOT}:{lid}"))
        elif SETTINGS.get_tag_extend_lot_id(lc.tag):
            kb.add(B("🔄 Лот-продление (используется общее по тегу)", None, f"{CBT.LOT_EXTEND_LOT}:{lid}"))
        else:
            kb.add(B("🔄 Лот-продление (не задан)", None, f"{CBT.LOT_EXTEND_LOT}:{lid}"))
        if fp_active is True:
            kb.add(B("🔴 Выключить", None, f"{CBT.LOT_TOGGLE_FP}:{lid}:0"))
        elif fp_active is False:
            kb.add(B("🟢 Включить", None, f"{CBT.LOT_TOGGLE_FP}:{lid}:1"))
        else:
            kb.add(B("⚡ Вкл/Выкл", None, f"{CBT.LOT_TOGGLE_FP}:{lid}:toggle"))
        kb.add(B("🗑 Удалить", None, f"{CBT.LOT_DEL_CONFIRM}:{lid}"))
        kb.add(B("⬅️ К списку", None, CBT.LOTS_FIXED if lc.lot_type == "fixed" else CBT.LOTS_HOURLY))
        edit(c.message, text, kb)

    def lot_rename(c):
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        _temp_storage.setdefault(c.from_user.id, {})["rename_lot_old"] = lid
        answer(c)
        _ask(c.message.chat.id, c.from_user.id, States.LOT_RENAME,
             f"🔢 Текущий ID: <code>{lid}</code>\n\nВведите <b>новый ID лота</b>:",
             _back_kb(f"{CBT.LOT_DETAIL}:{lid}"))

    def _h_lot_rename(m):
        raw = (m.text or "").strip()
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        old_id = _temp_storage.get(m.from_user.id, {}).get("rename_lot_old")
        if not old_id:
            send(m.chat.id, "❌ Данные утеряны", _main_kb())
            return
        new_id = _extract_lot_id(raw)
        if not new_id:
            send(m.chat.id, "❌ Не удалось распознать ID лота.", _back_kb(f"{CBT.LOT_DETAIL}:{old_id}"))
            return
        if SETTINGS.has_lot(new_id):
            send(m.chat.id, f"❌ Лот <code>{new_id}</code> уже существует", _back_kb(f"{CBT.LOT_DETAIL}:{old_id}"))
            return
        ok = SETTINGS.rename_lot(old_id, new_id)
        if ok:
            _invalidate_lots_cache()
            send(m.chat.id, f"✅ ID изменён: <code>{old_id}</code> → <code>{new_id}</code>",
                 _back_kb(f"{CBT.LOT_DETAIL}:{new_id}"))
        else:
            send(m.chat.id, "❌ Не удалось", _back_kb(f"{CBT.LOT_DETAIL}:{old_id}"))

    def lot_note(c):
        """Открыть диалог ввода заметки для лота"""
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        _temp_storage.setdefault(c.from_user.id, {})["note_lot_id"] = lid
        answer(c)
        cur_note = lc.note or "(не задана)"
        _ask(c.message.chat.id, c.from_user.id, States.LOT_NOTE,
             f"📝 <b>Заметка для лота #{lid}</b>\n\n"
             f"Текущая: <code>{cur_note}</code>\n\n"
             f"Введите новую заметку (или <code>0</code> чтобы убрать).\n"
             f"<i>Заметка только для вашего удобства — не влияет на работу плагина.</i>",
             _back_kb(f"{CBT.LOT_DETAIL}:{lid}"))

    def _h_lot_note(m):
        uid = m.from_user.id
        lid = (_temp_storage.get(uid) or {}).get("note_lot_id")
        _cleanup_dialog(m.chat.id, uid, m.message_id)
        if not lid:
            send(m.chat.id, "❌ Данные утеряны", _main_kb())
            return
        val = (m.text or "").strip()
        note = None if val in ("0", "", "убрать", "нет") else val
        SETTINGS.set_lot_note(lid, note or "")
        if note:
            send(m.chat.id, f"✅ Заметка для лота <code>{lid}</code> сохранена: <code>{note}</code>",
                 _back_kb(f"{CBT.LOT_DETAIL}:{lid}"))
        else:
            send(m.chat.id, f"✅ Заметка для лота <code>{lid}</code> удалена",
                 _back_kb(f"{CBT.LOT_DETAIL}:{lid}"))

    def lot_extend_lot_menu(c):
        """Меню управления лот-продлением для конкретного лота."""
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        answer(c)
        if lc.lot_type == "fixed":
            return _open_extopt_list(c, lid)
        cur = lc.extend_lot_id
        cur_str = f"<code>#{cur}</code>" if cur else "<i>не задан</i>"
        # Есть ли активный таймер
        timer_str = ""
        if cur:
            with _extend_lot_timers_lock:
                if cur in _extend_lot_timers:
                    timer_str = f"\n⏱ <i>Сейчас ожидает покупки (таймер активен)</i>"
        text = (
            f"🔄 <b>Лот-продление для лота #{lid}</b>\n\n"
            f"∟ Тег: <code>{lc.tag}</code>\n"
            f"∟ Лот-продление: {cur_str}{timer_str}\n\n"
            f"<b>Как работает:</b>\n"
            f"1. Покупатель пишет <code>!продлить</code>\n"
            f"2. Плагин включает лот-продление и отправляет ссылку\n"
            f"3. Если покупатель оплачивает — аренда продлевается, лот выключается\n"
            f"4. Если не оплачивает за 5 минут — лот выключается автоматически"
        )
        kb = K(row_width=1)
        if cur:
            kb.add(B(f"✏️ Изменить (текущий: #{cur})", None, f"{CBT.LOT_EXTEND_LOT_SET}:{lid}"))
            kb.add(B("🗑 Удалить лот-продление", None, f"{CBT.LOT_EXTEND_LOT_DEL}:{lid}"))
        else:
            kb.add(B("➕ Привязать лот-продление", None, f"{CBT.LOT_EXTEND_LOT_SET}:{lid}"))
        kb.add(B("⬅️ Назад", None, f"{CBT.LOT_DETAIL}:{lid}"))
        edit(c.message, text, kb)

    def _open_extopt_list(c, lid):
        """Список вариантов продления (разное время) для лота типа 'fixed'."""
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        opts = lc.extend_options
        lines = []
        for o in opts:
            o_id = str(o.get("lot_id"))
            timer_mark = ""
            with _extend_lot_timers_lock:
                if o_id in _extend_lot_timers:
                    timer_mark = " ⏱ <i>(ожидает покупки)</i>"
            lines.append(f"∟ <code>{_fmt_hours(o.get('hours'))}ч</code> → лот <code>#{o_id}</code>{timer_mark}")
        opts_str = "\n".join(lines) if lines else "<i>вариантов пока нет</i>"
        text = (
            f"🔄 <b>Лоты-продления для лота #{lid}</b>\n\n"
            f"∟ Тег: <code>{lc.tag}</code>\n\n"
            f"{opts_str}\n\n"
            f"<b>Как работает:</b>\n"
            f"1. Покупатель пишет <code>!продлить</code>\n"
            f"2. Плагин присылает список доступных вариантов времени продления\n"
            f"3. Покупатель пишет число часов в чат — плагин включает нужный лот и присылает ссылку\n"
            f"4. Если не оплачивает за 5 минут — лот выключается автоматически"
        )
        kb = K(row_width=1)
        for o in opts:
            o_id = str(o.get("lot_id"))
            kb.add(B(f"🗑 Удалить {_fmt_hours(o.get('hours'))}ч (#{o_id})", None, f"{CBT.LOT_EXTOPT_DEL}:{lid}:{o_id}"))
        kb.add(B("➕ Добавить вариант", None, f"{CBT.LOT_EXTOPT_ADD}:{lid}"))
        kb.add(B("⬅️ Назад", None, f"{CBT.LOT_DETAIL}:{lid}"))
        edit(c.message, text, kb)

    def lot_extopt_add(c):
        """Начало добавления нового варианта продления: сперва ID лота, потом часы."""
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        _temp_storage.setdefault(c.from_user.id, {})["extopt_main_id"] = lid
        answer(c)
        _ask(
            c.message.chat.id, c.from_user.id, States.EXTOPT_LOT_ID,
            f"🔄 <b>Новый вариант продления для лота #{lid}</b>\n\n"
            f"Введите <b>ID лота-продления</b> или ссылку на него.",
            _back_kb(f"{CBT.LOT_EXTEND_LOT}:{lid}")
        )

    def _h_extopt_lot_id(m):
        raw = (m.text or "").strip()
        uid = m.from_user.id
        main_lot_id = (_temp_storage.get(uid) or {}).get("extopt_main_id")
        _cleanup_dialog(m.chat.id, uid, m.message_id)
        if not main_lot_id:
            send(m.chat.id, "❌ Данные утеряны", _main_kb())
            return
        extend_id = _extract_lot_id(raw)
        if not extend_id:
            send(m.chat.id, "❌ Не удалось распознать ID лота",
                 _back_kb(f"{CBT.LOT_EXTEND_LOT}:{main_lot_id}"))
            return
        if extend_id == main_lot_id:
            send(m.chat.id, "❌ Лот-продление не может совпадать с основным лотом",
                 _back_kb(f"{CBT.LOT_EXTEND_LOT}:{main_lot_id}"))
            return
        _temp_storage.setdefault(uid, {})["extopt_ext_id"] = extend_id
        _ask(
            m.chat.id, uid, States.EXTOPT_HOURS,
            f"⏱ Введите <b>время в часах</b>, которое даёт лот <code>#{extend_id}</code> "
            f"(например: 5 или 1.5):",
            _back_kb(f"{CBT.LOT_EXTEND_LOT}:{main_lot_id}")
        )

    def _h_extopt_hours(m):
        raw = (m.text or "").strip().replace(",", ".")
        uid = m.from_user.id
        temp = _temp_storage.get(uid) or {}
        main_lot_id = temp.get("extopt_main_id")
        extend_id = temp.get("extopt_ext_id")
        _cleanup_dialog(m.chat.id, uid, m.message_id)
        if not main_lot_id or not extend_id:
            send(m.chat.id, "❌ Данные утеряны", _main_kb())
            return
        try:
            hours_val = float(raw)
            if hours_val <= 0:
                raise ValueError
        except ValueError:
            send(m.chat.id, "❌ Введите положительное число часов",
                 _back_kb(f"{CBT.LOT_EXTEND_LOT}:{main_lot_id}"))
            return
        SETTINGS.add_lot_extend_option(main_lot_id, extend_id, hours_val)
        _invalidate_lots_cache()
        msg_sent = send(
            m.chat.id,
            f"⏳ Привязываю вариант продления <code>{_fmt_hours(hours_val)}ч</code> → "
            f"<code>#{extend_id}</code> к лоту <code>#{main_lot_id}</code>...")

        def _write_tag_bg(mid=m.chat.id, main_id=main_lot_id, ext_id=extend_id,
                           hrs=hours_val, prev_msg=msg_sent):
            try:
                target_tag = SETTINGS.ensure_match_tag(main_id)
                ok, tag_msg = (False, "Не удалось создать ID для основного лота")
                if target_tag:
                    ok, tag_msg = _write_tag_to_funpay_lot(cardinal_ref, ext_id, target_tag)
                if ok:
                    result_text = (
                        f"✅ Вариант продления <code>{_fmt_hours(hrs)}ч</code> → <code>#{ext_id}</code> "
                        f"добавлен для лота <code>#{main_id}</code>\n"
                        f"∟ Тег <code>#{target_tag}</code> записан в описание лота-продления\n"
                        f"∟ {tag_msg}"
                    )
                else:
                    result_text = (
                        f"⚠️ Вариант добавлен, но НЕ удалось записать тег в описание "
                        f"лота <code>#{ext_id}</code>: {tag_msg}\n\n"
                        f"Допишите вручную <code>#{target_tag or '???'}</code> в конец подробного "
                        f"описания этого лота на FunPay, либо нажмите «🏷 Авто ID» ещё раз."
                    )
                    if tg_logs:
                        tg_logs.error(
                            f"Вариант продления #{ext_id} → #{main_id}: тег не записан ({tag_msg})")
                try:
                    edit(prev_msg, result_text, _back_kb(f"{CBT.LOT_EXTEND_LOT}:{main_id}"))
                except Exception:
                    send(mid, result_text, _back_kb(f"{CBT.LOT_EXTEND_LOT}:{main_id}"))
            except Exception as e:
                code, desc, _ = _classify_error(e)
                logger.error(f"[ASRplus] Ошибка добавления варианта продления #{ext_id}: [{code}] {desc}")
                send(mid, f"❌ Ошибка привязки: {desc}", _back_kb(f"{CBT.LOT_EXTEND_LOT}:{main_id}"))

        threading.Thread(target=_write_tag_bg, daemon=True).start()

    def lot_extopt_del(c):
        """Удалить конкретный вариант продления."""
        parts = c.data.split(":")
        lid, ext_id = parts[1], parts[2]
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        _cancel_extend_lot_timer(ext_id)
        SETTINGS.remove_lot_extend_option(lid, ext_id)
        _invalidate_lots_cache()
        answer(c, f"✅ Вариант #{ext_id} удалён")
        _open_extopt_list(c, lid)

    # ==================== Продление по тегам ====================

    def open_tag_extend_list(c):
        """Список тегов, для которых можно настроить общее продление."""
        tags = SETTINGS.tags_with_lots() or AccountRepo.all_tags()
        kb = K(row_width=1)
        for tag in sorted(set(tags)):
            has_single = bool(SETTINGS.get_tag_extend_lot_id(tag))
            n_opts = len(SETTINGS.get_tag_extend_options(tag))
            mark = "🔄" if (has_single or n_opts) else "➕"
            suffix = ""
            if has_single:
                suffix = f" · #{SETTINGS.get_tag_extend_lot_id(tag)}"
            elif n_opts:
                suffix = f" · {n_opts} вариант(а)"
            kb.add(B(f"{mark} {tag}{suffix}", None, f"{CBT.TAG_EXTEND_DETAIL}:{tag}"))
        kb.add(B("⬅️ Назад", None, CBT.LOTS))
        text = (
            "<b>🏷 Продление по тегам</b>\n\n"
            "Настройте лот-продление один раз на весь тег (пул аккаунтов) — "
            "тогда все лоты этого тега, у которых нет своего индивидуального "
            "продления, будут пользоваться этим общим.\n\n"
            "∟ Для почасовых лотов — один общий лот-продление.\n"
            "∟ Для фиксированных — можно задать несколько вариантов с разным временем."
        )
        if not tags:
            text += "\n\n<i>Нет тегов с настроенными лотами.</i>"
        edit(c.message, text, kb)

    def open_tag_extend_detail(c):
        tag = _p(c)
        single = SETTINGS.get_tag_extend_lot_id(tag)
        opts = SETTINGS.get_tag_extend_options(tag)
        opt_lines = []
        for o in opts:
            o_id = str(o.get("lot_id"))
            timer_mark = ""
            with _extend_lot_timers_lock:
                if o_id in _extend_lot_timers:
                    timer_mark = " ⏱ <i>(ожидает покупки)</i>"
            opt_lines.append(f"∟ <code>{_fmt_hours(o.get('hours'))}ч</code> → лот <code>#{o_id}</code>{timer_mark}")
        opts_str = "\n".join(opt_lines) if opt_lines else "<i>вариантов пока нет</i>"
        single_str = f"<code>#{single}</code>" if single else "<i>не задан</i>"
        text = (
            f"🏷 <b>Продление для тега «{tag}»</b>\n\n"
            f"<b>Общий лот-продление (для почасовых лотов):</b>\n"
            f"∟ {single_str}\n\n"
            f"<b>Варианты по времени (для фиксированных лотов):</b>\n"
            f"{opts_str}\n\n"
            f"<i>Действует для всех лотов с тегом «{tag}», у которых не задано "
            f"своё индивидуальное продление в настройках лота.</i>"
        )
        kb = K(row_width=1)
        if single:
            kb.add(B(f"✏️ Изменить общий (#{single})", None, f"{CBT.TAG_EXTEND_LOT_SET}:{tag}"))
            kb.add(B("🗑 Удалить общий", None, f"{CBT.TAG_EXTEND_LOT_DEL}:{tag}"))
        else:
            kb.add(B("➕ Задать общий лот-продление", None, f"{CBT.TAG_EXTEND_LOT_SET}:{tag}"))
        for o in opts:
            o_id = str(o.get("lot_id"))
            kb.add(B(f"🗑 Удалить вариант {_fmt_hours(o.get('hours'))}ч (#{o_id})", None,
                     f"{CBT.TAG_EXTOPT_DEL}:{tag}:{o_id}"))
        kb.add(B("➕ Добавить вариант по времени", None, f"{CBT.TAG_EXTOPT_ADD}:{tag}"))
        kb.add(B("⬅️ Назад", None, CBT.TAG_EXTEND))
        edit(c.message, text, kb)

    def tag_extend_lot_set(c):
        tag = _p(c)
        _temp_storage.setdefault(c.from_user.id, {})["tag_extend_tag"] = tag
        answer(c)
        _ask(
            c.message.chat.id, c.from_user.id, States.TAG_EXTEND_LOT_ID,
            f"🔄 <b>Общий лот-продление для тега «{tag}»</b>\n\n"
            f"Введите <b>ID лота-продления</b> или ссылку на него.\n\n"
            f"<i>Он будет включаться для любого лота с этим тегом, у которого нет "
            f"своего индивидуального лота-продления.</i>",
            _back_kb(f"{CBT.TAG_EXTEND_DETAIL}:{tag}")
        )

    def _h_tag_extend_lot_id(m):
        raw = (m.text or "").strip()
        uid = m.from_user.id
        tag = (_temp_storage.get(uid) or {}).get("tag_extend_tag")
        _cleanup_dialog(m.chat.id, uid, m.message_id)
        if not tag:
            send(m.chat.id, "❌ Данные утеряны", _main_kb())
            return
        extend_id = _extract_lot_id(raw)
        if not extend_id:
            send(m.chat.id, "❌ Не удалось распознать ID лота",
                 _back_kb(f"{CBT.TAG_EXTEND_DETAIL}:{tag}"))
            return
        SETTINGS.set_tag_extend_lot_id(tag, extend_id)
        _invalidate_lots_cache()
        send(m.chat.id,
             f"✅ Общий лот-продление <code>#{extend_id}</code> задан для тега «{tag}»\n\n"
             f"⚠️ Не забудьте, что каждый лот, которым он должен опознаваться как "
             f"продление, должен иметь корректно настроенный ID (кнопка «🏷 Авто ID» "
             f"в настройках лота) — как и при индивидуальной привязке.",
             _back_kb(f"{CBT.TAG_EXTEND_DETAIL}:{tag}"))

    def tag_extend_lot_del(c):
        tag = _p(c)
        cur = SETTINGS.get_tag_extend_lot_id(tag)
        if not cur:
            return answer(c, "ℹ️ Общий лот-продление не задан", True)
        _cancel_extend_lot_timer(cur)
        SETTINGS.set_tag_extend_lot_id(tag, None)
        _invalidate_lots_cache()
        answer(c, f"✅ Общий лот-продление #{cur} удалён")
        c.data = f"{CBT.TAG_EXTEND_DETAIL}:{tag}"
        open_tag_extend_detail(c)

    def tag_extopt_add(c):
        tag = _p(c)
        _temp_storage.setdefault(c.from_user.id, {})["tag_extopt_tag"] = tag
        answer(c)
        _ask(
            c.message.chat.id, c.from_user.id, States.TAG_EXTOPT_LOT_ID,
            f"🔄 <b>Новый вариант продления для тега «{tag}»</b>\n\n"
            f"Введите <b>ID лота-продления</b> или ссылку на него.",
            _back_kb(f"{CBT.TAG_EXTEND_DETAIL}:{tag}")
        )

    def _h_tag_extopt_lot_id(m):
        raw = (m.text or "").strip()
        uid = m.from_user.id
        tag = (_temp_storage.get(uid) or {}).get("tag_extopt_tag")
        _cleanup_dialog(m.chat.id, uid, m.message_id)
        if not tag:
            send(m.chat.id, "❌ Данные утеряны", _main_kb())
            return
        extend_id = _extract_lot_id(raw)
        if not extend_id:
            send(m.chat.id, "❌ Не удалось распознать ID лота",
                 _back_kb(f"{CBT.TAG_EXTEND_DETAIL}:{tag}"))
            return
        _temp_storage.setdefault(uid, {})["tag_extopt_ext_id"] = extend_id
        _ask(
            m.chat.id, uid, States.TAG_EXTOPT_HOURS,
            f"⏱ Введите <b>время в часах</b>, которое даёт лот <code>#{extend_id}</code> "
            f"(например: 5 или 1.5):",
            _back_kb(f"{CBT.TAG_EXTEND_DETAIL}:{tag}")
        )

    def _h_tag_extopt_hours(m):
        raw = (m.text or "").strip().replace(",", ".")
        uid = m.from_user.id
        temp = _temp_storage.get(uid) or {}
        tag = temp.get("tag_extopt_tag")
        extend_id = temp.get("tag_extopt_ext_id")
        _cleanup_dialog(m.chat.id, uid, m.message_id)
        if not tag or not extend_id:
            send(m.chat.id, "❌ Данные утеряны", _main_kb())
            return
        try:
            hours_val = float(raw)
            if hours_val <= 0:
                raise ValueError
        except ValueError:
            send(m.chat.id, "❌ Введите положительное число часов",
                 _back_kb(f"{CBT.TAG_EXTEND_DETAIL}:{tag}"))
            return
        SETTINGS.add_tag_extend_option(tag, extend_id, hours_val)
        _invalidate_lots_cache()
        send(m.chat.id,
             f"✅ Вариант продления <code>{_fmt_hours(hours_val)}ч</code> → <code>#{extend_id}</code> "
             f"добавлен для тега «{tag}»\n\n"
             f"⚠️ Не забудьте: у лота-продления в подробном описании должен быть ID "
             f"того лота, для которого он должен опознаваться (как и при "
             f"индивидуальной привязке через «🏷 Авто ID»).",
             _back_kb(f"{CBT.TAG_EXTEND_DETAIL}:{tag}"))

    def tag_extopt_del(c):
        parts = c.data.split(":")
        tag, ext_id = parts[1], parts[2]
        _cancel_extend_lot_timer(ext_id)
        SETTINGS.remove_tag_extend_option(tag, ext_id)
        _invalidate_lots_cache()
        answer(c, f"✅ Вариант #{ext_id} удалён")
        c.data = f"{CBT.TAG_EXTEND_DETAIL}:{tag}"
        open_tag_extend_detail(c)

    def lot_extend_lot_set(c):
        """Начало ввода ID лота-продления."""
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        _temp_storage.setdefault(c.from_user.id, {})["extend_lot_main_id"] = lid
        answer(c)
        _ask(
            c.message.chat.id, c.from_user.id, States.EXTEND_LOT_ID,
            f"🔄 <b>Привязка лот-продления к лоту #{lid}</b>\n\n"
            f"Введите <b>ID лота-продления</b> или ссылку на него.\n\n"
            f"<i>Этот лот будет включаться когда покупатель напишет !продлить "
            f"и выключаться через 5 минут или после оплаты.</i>",
            _back_kb(f"{CBT.LOT_EXTEND_LOT}:{lid}")
        )

    def _h_extend_lot_id(m):
        """Обработчик ввода ID лота-продления."""
        raw = (m.text or "").strip()
        uid = m.from_user.id
        main_lot_id = (_temp_storage.get(uid) or {}).get("extend_lot_main_id")
        _cleanup_dialog(m.chat.id, uid, m.message_id)
        if not main_lot_id:
            send(m.chat.id, "❌ Данные утеряны", _main_kb())
            return
        extend_id = _extract_lot_id(raw)
        if not extend_id:
            send(m.chat.id, "❌ Не удалось распознать ID лота",
                 _back_kb(f"{CBT.LOT_EXTEND_LOT}:{main_lot_id}"))
            return
        if extend_id == main_lot_id:
            send(m.chat.id, "❌ Лот-продление не может совпадать с основным лотом",
                 _back_kb(f"{CBT.LOT_EXTEND_LOT}:{main_lot_id}"))
            return
        SETTINGS.set_lot(main_lot_id,
                         SETTINGS.get_lot(main_lot_id).tag,
                         extend_lot_id=extend_id,
                         note=SETTINGS.get_lot(main_lot_id).note)
        _invalidate_lots_cache()
        # КЛЮЧЕВОЙ МОМЕНТ: раньше здесь только сохранялась связка ID в настройках,
        # но в описание самого лота-продления на FunPay #id основного лота НЕ
        # дописывался. Из-за этого бот не мог распознать заказ, пришедший именно
        # с лота-продления (и для почасовых, и для фиксированных лотов) — оплата
        # просто не определялась как продление. Теперь сразу пишем тег основного
        # лота в описание лота-продления через FunPay API.
        msg_sent = send(
            m.chat.id,
            f"⏳ Привязываю лот-продление <code>#{extend_id}</code> к лоту <code>#{main_lot_id}</code>...")

        def _write_tag_bg(mid=m.chat.id, main_id=main_lot_id, ext_id=extend_id, prev_msg=msg_sent):
            try:
                target_tag = SETTINGS.ensure_match_tag(main_id)
                ok, tag_msg = (False, "Не удалось создать ID для основного лота")
                if target_tag:
                    ok, tag_msg = _write_tag_to_funpay_lot(cardinal_ref, ext_id, target_tag)
                if ok:
                    result_text = (
                        f"✅ Лот-продление <code>#{ext_id}</code> привязан к лоту <code>#{main_id}</code>\n"
                        f"∟ Тег <code>#{target_tag}</code> записан в описание лота-продления\n"
                        f"∟ {tag_msg}"
                    )
                else:
                    result_text = (
                        f"⚠️ Лот-продление <code>#{ext_id}</code> привязан к лоту <code>#{main_id}</code>,\n"
                        f"но НЕ удалось записать тег в его описание: {tag_msg}\n\n"
                        f"Заказы с этого лота-продления могут не определяться! "
                        f"Допишите вручную <code>#{target_tag or '???'}</code> в конец подробного "
                        f"описания лота <code>#{ext_id}</code> на FunPay, либо нажмите «🏷 Авто ID» "
                        f"ещё раз."
                    )
                    if tg_logs:
                        tg_logs.error(
                            f"Лот-продление #{ext_id} → #{main_id}: тег не записан ({tag_msg})")
                try:
                    edit(prev_msg, result_text, _back_kb(f"{CBT.LOT_DETAIL}:{main_id}"))
                except Exception:
                    send(mid, result_text, _back_kb(f"{CBT.LOT_DETAIL}:{main_id}"))
            except Exception as e:
                code, desc, _ = _classify_error(e)
                logger.error(f"[ASRplus] Ошибка записи тега для лота-продления #{ext_id}: [{code}] {desc}")
                send(mid, f"❌ Ошибка привязки: {desc}", _back_kb(f"{CBT.LOT_DETAIL}:{main_id}"))

        threading.Thread(target=_write_tag_bg, daemon=True).start()

    def lot_extend_lot_del(c):
        """Удалить привязку лот-продления."""
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        if not lc.extend_lot_id:
            return answer(c, "ℹ️ Лот-продление не задан", True)
        old_extend = lc.extend_lot_id
        # Отменяем таймер если активен
        _cancel_extend_lot_timer(old_extend)
        SETTINGS.set_lot(lid, lc.tag, extend_lot_id=None, note=lc.note)
        _invalidate_lots_cache()
        answer(c, f"✅ Лот-продление #{old_extend} отвязан")
        c.data = f"{CBT.LOT_DETAIL}:{lid}"
        open_lot_detail(c)

    def lot_del_confirm(c):
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        text = (f"⚠️ <b>Удалить лот?</b>\n\n∟ ID: <code>{lid}</code>\n∟ Тег: <code>{lc.tag}</code>\n\n❗ Необратимо!")
        kb = K(row_width=2)
        kb.add(B("✅ Да", None, f"{CBT.LOT_DEL_YES}:{lid}"), B("❌ Нет", None, f"{CBT.LOT_DEL_NO}:{lid}"))
        edit(c.message, text, kb)

    def lot_del_yes(c):
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        name = f"#{lid} ({lc.tag})" if lc else f"#{lid}"
        lot_type = lc.lot_type if lc else "hourly"
        SETTINGS.del_lot(lid)
        _invalidate_lots_cache()
        answer(c, f"✅ Лот {name} удалён")
        if lot_type == "fixed":
            open_lots_fixed(c)
        else:
            open_lots_hourly(c)

    def lot_del_no(c):
        lid = _p(c)
        answer(c, "❌ Отменено")
        c.data = f"{CBT.LOT_DETAIL}:{lid}"
        open_lot_detail(c)

    def lot_edit(c):
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Не найден", True)
        _temp_storage.setdefault(c.from_user.id, {})["edit_lot_id"] = lid
        _temp_storage[c.from_user.id]["edit_lot_tag"] = lc.tag
        tags = AccountRepo.all_tags()
        if not tags:
            return answer(c, "❌ Нет аккаунтов!", True)
        kb = K(row_width=2)
        for tag in tags:
            prefix = "✅ " if tag == lc.tag else ""
            kb.add(B(f"{prefix}{tag}", None, f"{CBT.LOT_EDIT_TAG}:{lid}:{tag}"))
        kb.add(B("⬅️ Назад", None, f"{CBT.LOT_DETAIL}:{lid}"))
        edit(c.message, f"✏️ <b>Изменить лот #{lid}</b>\n\nТекущий тег: <code>{lc.tag}</code>\n\nВыберите новый тег:", kb)

    def lot_edit_tag(c):
        parts = c.data.split(":")
        lid = parts[1]
        new_tag = _ntag(parts[2])
        SETTINGS.set_lot(lid, new_tag)
        _invalidate_lots_cache()
        answer(c, f"✅ Тег изменён на {new_tag}")
        c.data = f"{CBT.LOT_DETAIL}:{lid}"
        open_lot_detail(c)

    def lot_auto_tag(c):
        """
        Кнопка «Авто ID»: дописывает уникальный служебный ID лота в конец
        подробного описания на FunPay (для лотов, привязанных ещё до
        появления автозаписи ID при добавлении лота).

        Заодно чинит уже привязанный лот-продление (если он есть): раньше при
        привязке через "Лот-продление" тег в его описание не записывался, из-за
        чего заказы с лота-продления не распознавались. Эта кнопка позволяет
        починить такие старые привязки одним нажатием, без пересоздания связки.
        """
        lid = _p(c)
        answer(c, "⏳ Добавляю ID в описание лота...")
        ok, tag, msg = _auto_write_match_tag(cardinal_ref, lid)
        extra = ""
        lc = SETTINGS.get_lot(lid)
        if ok and lc and lc.extend_lot_id:
            ok_ext, ext_msg = _write_tag_to_funpay_lot(cardinal_ref, lc.extend_lot_id, tag)
            if ok_ext:
                extra = f"\n🔄 Лот-продление #{lc.extend_lot_id}: {ext_msg}"
            else:
                extra = f"\n⚠️ Лот-продление #{lc.extend_lot_id}: не удалось записать тег ({ext_msg})"
        if ok:
            answer(c, f"✅ {msg}{extra}"[:200])
        else:
            answer(c, f"❌ {msg}"[:200], True)
        c.data = f"{CBT.LOT_DETAIL}:{lid}"
        open_lot_detail(c)

    def lot_toggle_fp(c):
        parts = c.data.split(":")
        lid = parts[1]
        action = parts[2] if len(parts) > 2 else "toggle"
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        try:
            lf = cardinal_ref.account.get_lot_fields(int(lid))
            if action == "toggle":
                lf.active = not lf.active
            else:
                lf.active = bool(int(action))
            cardinal_ref.account.save_lot(lf)
            _invalidate_lots_cache()
            state = "🟢 включён" if lf.active else "🔴 выключен"
            answer(c, f"✅ Лот #{lid} {state}")
        except Exception as e:
            answer(c, f"❌ Ошибка: {_safe_err(e)}", True)
            return
        c.data = f"{CBT.LOT_DETAIL}:{lid}"
        open_lot_detail(c)

    def lots_disable_all(c):
        answer(c)
        edit(c.message, "⏳ Выключаю лоты...", _back_kb(CBT.LOTS))
        chat_id = c.message.chat.id
        def _do():
            tags = list({_ntag((SETTINGS.get_lot(lid) or LotConfig(tag="default")).tag) for lid in SETTINGS.lots})
            total = []
            for tag in tags:
                total.extend(_toggle_fp_lots_for_tag(cardinal_ref, tag, False))
            if total and tg_logs:
                tg_logs.lots_auto_disabled("all", total)
            send(chat_id, f"🔴 Выключено лотов: {len(total)}" if total else "ℹ️ Нечего выключать")
        threading.Thread(target=_do, daemon=True).start()

    def lots_enable_all(c):
        answer(c)
        edit(c.message, "⏳ Включаю лоты...", _back_kb(CBT.LOTS))
        chat_id = c.message.chat.id
        def _do():
            tags = list({_ntag((SETTINGS.get_lot(lid) or LotConfig(tag="default")).tag) for lid in SETTINGS.lots})
            total = []
            for tag in tags:
                total.extend(_toggle_fp_lots_for_tag(cardinal_ref, tag, True))
            if total and tg_logs:
                tg_logs.lots_auto_enabled("all", total)
            send(chat_id, f"🟢 Включено лотов: {len(total)}" if total else "ℹ️ Нечего включать")
        threading.Thread(target=_do, daemon=True).start()

    def lot_add(c):
        answer(c)
        lot_type = _p(c) or "hourly"
        if lot_type not in ("hourly", "fixed"):
            lot_type = "hourly"
        _temp_storage.setdefault(c.from_user.id, {})["add_lot_type"] = lot_type
        back_cb = CBT.LOTS_FIXED if lot_type == "fixed" else CBT.LOTS_HOURLY
        _ask(c.message.chat.id, c.from_user.id, States.LOT_ID,
             "Введите <b>ID лота</b> или ссылку на лот:", _back_kb(back_cb))

    def _h_lot_id(m):
        raw = (m.text or "").strip()
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        lot_type = _temp_storage.get(m.from_user.id, {}).get("add_lot_type", "hourly")
        back_cb = CBT.LOTS_FIXED if lot_type == "fixed" else CBT.LOTS_HOURLY
        lot_id = _extract_lot_id(raw)
        if not lot_id:
            send(m.chat.id, "❌ Не удалось распознать ID.", _back_kb(back_cb))
            return
        if SETTINGS.has_lot(lot_id):
            send(m.chat.id, f"❌ Лот <code>{lot_id}</code> уже добавлен", _back_kb(back_cb))
            return
        tags = AccountRepo.all_tags()
        if not tags:
            send(m.chat.id, "❌ Сначала добавьте аккаунты!", _main_kb())
            return
        _temp_storage.setdefault(m.from_user.id, {})["lot_id"] = lot_id
        _temp_storage[m.from_user.id]["add_lot_type"] = lot_type
        kb = K()
        for tag in tags:
            kb.add(B(tag, None, f"{CBT.LOT_TAG}:{tag}"))
        kb.add(B("⬅️ Назад", None, back_cb))
        send(m.chat.id, "Выберите <b>тег</b> для лота:", kb)

    def _finalize_lot_add(chat_id, lid, tag, lot_type, sub_id, fixed_hours=None, edit_msg=None):
        SETTINGS.set_lot(str(lid), tag, subcategory_id=sub_id, lot_type=lot_type, fixed_hours=fixed_hours)
        _invalidate_lots_cache()
        type_label = "📌 Фиксированный" if lot_type == "fixed" else "⏱ Почасовой"
        back_cb = CBT.LOTS_FIXED if lot_type == "fixed" else CBT.LOTS_HOURLY
        # Сразу дописываем уникальный служебный ID лота в подробное
        # описание на FunPay через API — вручную ничего нажимать не нужно.
        ok, mtag, msg = _auto_write_match_tag(cardinal_ref, str(lid))
        if ok:
            id_line = f"\n🆔 ID лота: <code>#{mtag}</code> (автоматически добавлен в описание)"
        else:
            id_line = (
                f"\n⚠️ Не удалось автоматически дописать ID в описание лота ({msg}). "
                f"Откройте лот и нажмите «🏷 Авто ID» вручную."
            )
        hours_line = f"\n⏱ Время аренды: <code>{fixed_hours}ч</code>" if lot_type == "fixed" else ""
        text = f"✅ Лот {lid} ({type_label}) привязан к тегу <code>{tag}</code>{hours_line}{id_line}"
        if edit_msg is not None:
            edit(edit_msg, text, _back_kb(back_cb))
        else:
            send(chat_id, text, _back_kb(back_cb))

    def lot_tag(c):
        tag = _ntag(_p(c))
        uid = c.from_user.id
        lid = _temp_storage.get(uid, {}).get("lot_id")
        lot_type = _temp_storage.get(uid, {}).get("add_lot_type", "hourly")
        if not lid:
            return answer(c, "❌ Данные утеряны", True)
        sub_id = None
        try:
            fp_lots = _get_cached_lots(cardinal_ref)
            fp_lot = next((l for l in fp_lots if str(l.id) == str(lid)), None)
            if fp_lot:
                sub = getattr(fp_lot, "subcategory", None)
                sub_id = getattr(sub, "id", None) if sub else None
        except Exception:
            pass
        if lot_type == "fixed":
            # Для фиксированного лота время аренды нужно указать один раз —
            # эти данные обязательны и реально используются при выдаче аккаунта.
            _temp_storage[uid]["add_lot_tag"] = tag
            _temp_storage[uid]["add_lot_sub_id"] = sub_id
            answer(c)
            _ask(c.message.chat.id, uid, States.LOT_FIXED_HOURS_ADD,
                 f"📌 Лот <code>{lid}</code> привязан к тегу <code>{tag}</code>.\n\n"
                 f"⏱ Укажите <b>время аренды в часах</b> для этого фиксированного лота "
                 f"(например: 24 или 12.5).\n"
                 f"<i>Это время выдаётся покупателю при заказе. Его можно изменить позже "
                 f"в настройках лота.</i>",
                 _back_kb(CBT.LOTS_FIXED))
            return
        _finalize_lot_add(c.message.chat.id, lid, tag, lot_type, sub_id, fixed_hours=None, edit_msg=c.message)

    def _h_lot_fixed_hours_add(m):
        uid = m.from_user.id
        d = _temp_storage.get(uid, {})
        lid = d.get("lot_id")
        tag = d.get("add_lot_tag")
        sub_id = d.get("add_lot_sub_id")
        _cleanup_dialog(m.chat.id, uid, m.message_id)
        if not lid or not tag:
            send(m.chat.id, "❌ Данные утеряны", _back_kb(CBT.LOTS_FIXED))
            return
        raw = (m.text or "").strip().replace(",", ".")
        try:
            hours = float(raw)
            if hours <= 0:
                raise ValueError
        except (ValueError, TypeError):
            send(m.chat.id, "❌ Введите положительное число часов (например: 24)",
                 _back_kb(CBT.LOTS_FIXED))
            return
        _finalize_lot_add(m.chat.id, lid, tag, "fixed", sub_id, fixed_hours=hours)

    def lot_fixed_hours(c):
        """Открыть диалог изменения времени фиксированного лота."""
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        _temp_storage.setdefault(c.from_user.id, {})["fixed_hours_lot_id"] = lid
        answer(c)
        cur = lc.fixed_hours if lc.fixed_hours else "не задано"
        _ask(c.message.chat.id, c.from_user.id, States.LOT_FIXED_HOURS,
             f"⏱ <b>Время аренды для лота #{lid}</b>\n\n"
             f"Текущее: <code>{cur}</code> ч\n\n"
             f"Введите новое время в часах (например: 24 или 12.5):",
             _back_kb(f"{CBT.LOT_DETAIL}:{lid}"))

    def _h_lot_fixed_hours(m):
        uid = m.from_user.id
        lid = (_temp_storage.get(uid) or {}).get("fixed_hours_lot_id")
        _cleanup_dialog(m.chat.id, uid, m.message_id)
        if not lid:
            send(m.chat.id, "❌ Данные утеряны", _main_kb())
            return
        raw = (m.text or "").strip().replace(",", ".")
        try:
            hours = float(raw)
            if hours <= 0:
                raise ValueError
        except (ValueError, TypeError):
            send(m.chat.id, "❌ Введите положительное число часов (например: 24)",
                 _back_kb(f"{CBT.LOT_DETAIL}:{lid}"))
            return
        SETTINGS.set_lot_fixed_hours(lid, hours)
        _invalidate_lots_cache()
        send(m.chat.id, f"✅ Время аренды для лота <code>{lid}</code> установлено: <code>{hours}ч</code>",
             _back_kb(f"{CBT.LOT_DETAIL}:{lid}"))

    def open_reviews(c):
        rules = SETTINGS.get_review_rules()
        kb = K(row_width=1)
        for r in rules:
            bl = f"{int(r.bonus_hours)}ч" if r.bonus_hours == int(r.bonus_hours) else f"{r.bonus_hours}ч"
            kb.add(B(f"🎁 {r.rent_hours}ч → +{bl} ❌", None, f"{CBT.REV_DEL}:{r.rent_hours}"))
        kb.add(B("➕ Добавить", None, CBT.REV_ADD))
        kb.add(B("⬅️ Назад", None, CBT.MAIN))
        txt = "<b>⭐️ Бонусы за отзывы</b>\n\n"
        if rules:
            txt += "".join(f"∟ от <code>{r.rent_hours}ч</code> → <code>+{r.bonus_hours}ч</code>\n" for r in rules)
            txt += "\nНажмите для удаления."
        else:
            txt += "Правил нет."
        edit(c.message, txt, kb)

    def rev_add(c):
        answer(c)
        _temp_storage.setdefault(c.from_user.id, {})
        _ask(c.message.chat.id, c.from_user.id, States.REV_HRS_CUSTOM,
             "⭐️ <b>Добавить бонус за отзыв</b>\n\n"
             "Введите <b>минимальное количество часов аренды</b> для получения бонуса:\n"
             "<i>Например: 5 или 10.5</i>",
             _back_kb(CBT.REVS))

    def _h_rev_hrs_custom(m):
        raw = (m.text or "").strip()
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        try:
            h = float(raw)
            if h <= 0:
                raise ValueError
        except (ValueError, TypeError):
            send(m.chat.id, "❌ Введите положительное число часов", _back_kb(CBT.REVS))
            return
        _temp_storage.setdefault(m.from_user.id, {})["rev_rh"] = h
        _ask(m.chat.id, m.from_user.id, States.REV_BON_CUSTOM,
             f"⭐️ Мин. часов аренды: <code>{h}</code>\n\n"
             "Теперь введите <b>количество бонусных часов</b>:\n"
             "<i>Например: 2 или 1.5</i>",
             _back_kb(CBT.REVS))

    def _h_rev_bon_custom(m):
        raw = (m.text or "").strip()
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        rh = _temp_storage.get(m.from_user.id, {}).get("rev_rh")
        if rh is None:
            send(m.chat.id, "❌ Данные утеряны", _back_kb(CBT.REVS))
            return
        try:
            bh = float(raw)
            if bh <= 0:
                raise ValueError
        except (ValueError, TypeError):
            send(m.chat.id, "❌ Введите положительное число часов", _back_kb(CBT.REVS))
            return
        SETTINGS.add_review_rule(int(rh) if rh == int(rh) else rh, bh)
        send(m.chat.id,
             f"✅ Добавлено правило: от <code>{rh}ч</code> аренды → бонус <code>+{bh}ч</code>",
             _back_kb(CBT.REVS))

    def rev_hours(c):
        h = _pid(c)
        _temp_storage.setdefault(c.from_user.id, {})["rev_rh"] = h
        kb = K(row_width=3)
        for bh in [1,2,3,6,12,24]:
            kb.add(B(f"{bh}ч", None, f"{CBT.REV_BON}:{bh}"))
        kb.add(B("⬅️", None, CBT.REVS))
        edit(c.message, f"Аренда от: <code>{h}ч</code>\n\n<b>Бонус (часов)</b>:", kb)

    def rev_bonus(c):
        bh = _pid(c)
        rh = _temp_storage.get(c.from_user.id, {}).get("rev_rh", 3)
        SETTINGS.add_review_rule(rh, float(bh))
        answer(c, f"✅ {rh}ч → +{bh}ч")
        open_reviews(c)

    def rev_del(c):
        SETTINGS.del_review_rule(_pid(c))
        open_reviews(c)

    def open_notifs(c):
        kb = K(row_width=1)
        for attr, label in [("notification_order_completed", "Выдача"),
                            ("notification_error", "Ошибки"),
                            ("notification_refund", "Возвраты"),
                            ("notification_preparing", "Сообщение о подготовке")]:
            kb.add(B(f"{_is_on(getattr(SETTINGS, attr))} {label}", None, f"{CBT.TOGGLE}:{attr}"))
        kb.add(B("⬅️ Назад", None, CBT.FUNCTIONS))
        edit(c.message, "<b>🔔 Уведомления</b>", kb)

    def open_msgs(c):
        kb = K(row_width=2)
        row_buf = []
        for key, desc in MessagesConfig.DESCRIPTIONS.items():
            btn = B(desc, None, f"{CBT.MSG_EDIT}:{key}")
            if len(desc) > 18:
                if row_buf:
                    kb.row(*row_buf)
                    row_buf = []
                kb.row(btn)
            else:
                row_buf.append(btn)
                if len(row_buf) == 2:
                    kb.row(*row_buf)
                    row_buf = []
        if row_buf:
            kb.row(*row_buf)
        kb.row(B("⬅️ Назад", None, CBT.FUNCTIONS))
        edit(c.message, "<b>💬 Тексты сообщений</b>", kb)

    def msg_edit(c):
        key = _p(c)
        _temp_storage.setdefault(c.from_user.id, {})["edit_key"] = key
        answer(c)
        cur = getattr(SETTINGS.messages, key, "")
        desc = MessagesConfig.DESCRIPTIONS.get(key, "")
        txt = (f"<b>{desc}</b>\n\nТекущий:\n<code>{cur}</code>\n\n"
               f"Переменные: $login $password $hours $code $end_time $remaining $id $link $stock_list $commands_list $accounts_list\n\nВведите новый текст:")
        kb = K().add(B("♻️ Сбросить по умолчанию", None, f"{CBT.MSG_RESET}:{key}"))
        kb.add(B("⬅️ Назад", None, CBT.MSGS))
        _ask(c.message.chat.id, c.from_user.id, States.MSG_EDIT, txt, kb)

    def msg_reset(c):
        """Вернуть текст сообщения к значению по умолчанию."""
        key = _p(c)
        _defaults = MessagesConfig()
        if not hasattr(_defaults, key):
            return answer(c, "❌ Неизвестное сообщение", True)
        default_val = getattr(_defaults, key)
        SETTINGS.set_message(key, default_val)
        tg.clear_state(c.message.chat.id, c.from_user.id, False)
        answer(c, "✅ Сброшено по умолчанию")
        desc = MessagesConfig.DESCRIPTIONS.get(key, "")
        edit(c.message, f"✅ <b>{desc}</b> возвращено к значению по умолчанию:\n\n<code>{default_val}</code>",
             _back_kb(CBT.MSGS))

    def _h_msg_edit(m):
        key = _temp_storage.get(m.from_user.id, {}).get("edit_key")
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        if key:
            SETTINGS.set_message(key, m.text.strip())
            send(m.chat.id, "✅ Сохранено!", _main_kb())
        else:
            send(m.chat.id, "❌ Данные утеряны", _main_kb())

    def open_stats(c):
        kb = K(row_width=1)
        kb.add(B("📈 Полная статистика", None, CBT.FULL_STATS))
        kb.row(B("👤 Активные аренды", None, CBT.ACTIVE_RENTS), B("🟢 Свободные аккаунты", None, CBT.FREE_ACCS))
        kb.add(B("🏷 Самые продаваемые теги", None, CBT.TOP_TAGS))
        kb.add(B("⬅️ Назад", None, CBT.MAIN))
        edit(c.message, _stats_text(), kb)

    def open_top_tags(c):
        answer(c)
        with _data_lock:
            orders_snapshot = list(ORDERS.values())
        finished = [o for o in orders_snapshot if o.status in (RentStatus.FINISHED, RentStatus.ACTIVE)]
        if not finished:
            return answer(c, "📭 Нет завершённых заказов для анализа", True)

        now_t = time.time()

        def _tag_block(name, from_ts):
            threshold = _fmt(MOSCOW_TZ.localize(datetime.fromtimestamp(from_ts)))
            arr = [o for o in finished if o.created_at >= threshold and o.status == RentStatus.FINISHED]
            if not arr:
                return f"— <b>{name}</b>\n  ∟ Нет данных\n"
            tag_stats: Dict[str, dict] = defaultdict(lambda: {"cnt": 0, "hrs": 0.0})
            for o in arr:
                tag = o.acc_tag or "default"
                tag_stats[tag]["cnt"] += 1
                tag_stats[tag]["hrs"] += o.hours
            top = sorted(tag_stats.items(), key=lambda x: x[1]["cnt"], reverse=True)[:10]
            lines = []
            for i, (tag, v) in enumerate(top, 1):
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"#{i}")
                lines.append(f"  {medal} <b>{tag}</b> — {v['cnt']} аренд | {v['hrs']:.0f}ч")
            return f"— <b>{name}</b>\n" + "\n".join(lines) + "\n"

        all_tags_stats: Dict[str, dict] = defaultdict(lambda: {"cnt": 0, "hrs": 0.0})
        for o in finished:
            if o.status == RentStatus.FINISHED:
                tag = o.acc_tag or "default"
                all_tags_stats[tag]["cnt"] += 1
                all_tags_stats[tag]["hrs"] += o.hours

        all_top = sorted(all_tags_stats.items(), key=lambda x: x[1]["cnt"], reverse=True)
        total_orders = sum(v["cnt"] for v in all_tags_stats.values())
        all_lines = []
        for i, (tag, v) in enumerate(all_top[:10], 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"#{i}")
            pct = (v["cnt"] / total_orders * 100) if total_orders else 0
            all_lines.append(f"  {medal} <b>{tag}</b> — {v['cnt']} ({pct:.0f}%) | {v['hrs']:.0f}ч")

        txt = (
            f"🏷 <b>Самые продаваемые теги</b>\n"
            f"Всего завершённых: {total_orders}\n\n"
            f"— <b>За всё время</b>\n" + "\n".join(all_lines) + "\n\n"
            + _tag_block("Сегодня", now_t - 86400) + "\n"
            + _tag_block("Неделя", now_t - 604800) + "\n"
            + _tag_block("Месяц", now_t - 2592000)
        )
        kb = K(row_width=1)
        kb.add(B("🔄 Обновить", None, CBT.TOP_TAGS), B("⬅️ Назад", None, CBT.STATS))
        edit(c.message, txt[:4000], kb)

    def open_active_rents(c):
        active = [o for o in ORDERS.values() if o.status == RentStatus.ACTIVE]
        if not active:
            return answer(c, "👤 Активных аренд нет", True)
        lines = []
        for o in sorted(active, key=lambda x: x.created_at):
            acc = AccountRepo.get(o.acc_id)
            remaining = _remaining_str(acc.rental_end) if acc and acc.rental_end else "—"
            # Показываем статус подтверждения из pending_store
            p = _pending_store.get(o.id)
            confirmed_icon = "✅" if (p and p.confirmed) else "⏳"
            lines.append(
                f"∟ {confirmed_icon} <b>{o.buyer}</b> | <code>{o.acc_login or '—'}</code> [{o.acc_tag}]\n"
                f"   ⏱ осталось: {remaining}"
            )
        text = f"<b>👤 Активные аренды: {len(active)}</b>\n✅ подтверждён  ⏳ ещё не написал\n\n" + "\n\n".join(lines)
        kb = K(row_width=1)
        kb.add(B("🔄 Обновить", None, CBT.ACTIVE_RENTS), B("⬅️ Назад", None, CBT.STATS))
        edit(c.message, text[:4000], kb)

    def open_pending_orders(c):
        """Заказы, по которым покупатель ещё не написал !код (не подтверждены)."""
        answer(c)
        snapshot = _pending_store.all_pending()
        if not snapshot:
            kb = K(row_width=1)
            kb.add(B("🔄 Обновить", None, CBT.PENDING_ORDERS), B("⬅️ Назад", None, CBT.STATS))
            edit(c.message, "⏳ <b>Ожидают подтверждения</b>\n\nВсе заказы уже подтверждены покупателями.", kb)
            return
        lines = []
        for p in sorted(snapshot, key=lambda x: x.received_at):
            acc_obj = next((a for a in ACCOUNTS if a.current_order == p.order_id), None)
            acc_name = acc_obj.login if acc_obj else "—"
            lines.append(
                f"∟ <b>{p.buyer}</b> [{p.tag}] | <code>{acc_name}</code>\n"
                f"   📦 {p.hours}ч | ⏱ ждём уже {p.age_str()}\n"
                f"   Заказ: <code>{p.order_id[:18]}...</code>"
            )
        text = (
            f"<b>⏳ Ожидают первого !код: {len(snapshot)}</b>\n"
            f"<i>Покупатели ещё не написали !код после выдачи аккаунта</i>\n\n"
            + "\n\n".join(lines)
        )
        kb = K(row_width=1)
        kb.add(B("🔄 Обновить", None, CBT.PENDING_ORDERS), B("⬅️ Назад", None, CBT.STATS))
        edit(c.message, text[:4000], kb)

    def open_free_accs(c):
        free = [a for a in ACCOUNTS if a.status == RentStatus.FREE]
        if not free:
            return answer(c, "🟢 Свободных аккаунтов нет", True)
        by_tag: Dict[str, list] = {}
        for a in free:
            by_tag.setdefault(_ntag(a.tag), []).append(a.login)
        lines = []
        for tag, logins in sorted(by_tag.items()):
            lines.append(f"<b>[{tag}]</b> — {len(logins)} шт.\n" + "\n".join(f"  ∟ <code>{l}</code>" for l in logins))
        text = f"<b>🟢 Свободные аккаунты: {len(free)}</b>\n\n" + "\n\n".join(lines)
        kb = K(row_width=1)
        kb.add(B("🔄 Обновить", None, CBT.FREE_ACCS), B("⬅️ Назад", None, CBT.STATS))
        edit(c.message, text[:4000], kb)

    def open_full_stats(c):
        now_t = time.time()
        finished = [o for o in ORDERS.values() if o.status == RentStatus.FINISHED]
        def make_block(name, from_ts):
            threshold = _fmt(MOSCOW_TZ.localize(datetime.fromtimestamp(from_ts)))
            arr = [o for o in finished if o.created_at >= threshold]
            buyers = defaultdict(lambda: {"cnt": 0, "hrs": 0.0})
            accs = defaultdict(lambda: {"cnt": 0, "hrs": 0.0})
            for o in arr:
                buyers[o.buyer]["cnt"] += 1
                buyers[o.buyer]["hrs"] += o.hours
                label = o.acc_login or f"#{o.acc_id}"
                accs[label]["cnt"] += 1
                accs[label]["hrs"] += o.hours
            def fmt_top(dct):
                top = sorted(dct.items(), key=lambda x: x[1]["hrs"], reverse=True)[:5]
                return "\n".join(f"  ∟ {k}: {v['cnt']} | {v['hrs']:.0f}ч" for k, v in top) or "  ∟ Нет данных"
            cnt = len(arr)
            hrs = sum(o.hours for o in arr)
            return (f"— <b>{name}</b>\nВсего: {cnt} аренд | {hrs:.0f} ч\n"
                    f"Покупатели:\n{fmt_top(buyers)}\nАккаунты:\n{fmt_top(accs)}\n")
        all_cnt = len(finished)
        all_hrs = sum(o.hours for o in finished)
        txt = f"📈 <b>Полная статистика</b>\n{all_cnt} аренд | {all_hrs:.0f} ч\n\n"
        txt += "\n".join([make_block("Сегодня", now_t - 86400),
                          make_block("Неделя", now_t - 604800),
                          make_block("Месяц", now_t - 2592000)])
        edit(c.message, txt, _back_kb(CBT.STATS))

    def open_history(c):
        page = _pid(c)
        all_orders = sorted(
            [o for o in ORDERS.values() if o.status in (RentStatus.FINISHED, RentStatus.REFUND, RentStatus.ERROR, RentStatus.ACTIVE)],
            key=lambda x: x.created_at, reverse=True)
        total = len(all_orders)
        per = 10
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        sl = all_orders[(page - 1) * per:page * per]
        kb = K(row_width=1)
        for o in sl:
            icons = {"FINISHED": "✅", "REFUND": "💰", "ERROR": "❌", "ACTIVE": "👤"}
            icon = icons.get(o.status, "❓")
            ext = " 🔄" if o.is_extension else ""
            acc_name = o.acc_login or f"#{o.acc_id}"
            kb.add(B(f"{icon} {o.buyer} | {acc_name} | {o.hours}ч{ext}", None, f"{CBT.HIST_DETAIL}:{o.id}"))
        if pages > 1:
            nav = []
            if page > 1:
                nav.append(B("⬅️", None, f"{CBT.HIST}:{page - 1}"))
            nav.append(B(f"{page}/{pages}", None, _CBT.EMPTY))
            if page < pages:
                nav.append(B("➡️", None, f"{CBT.HIST}:{page + 1}"))
            kb.row(*nav)
        kb.row(B("🗑 Очистить историю", None, f"{CBT.HIST_CLEAR}:confirm"))
        kb.add(B("⬅️ Назад", None, CBT.FUNCTIONS))
        edit(c.message, f"<b>📜 История ({total})</b>", kb)

    def history_clear_confirm(c):
        answer(c)
        finished_count = sum(1 for o in ORDERS.values()
                             if o.status in (RentStatus.FINISHED, RentStatus.REFUND, RentStatus.ERROR))
        kb = K(row_width=2)
        kb.add(B("✅ Да, очистить", None, f"{CBT.HIST_CLEAR_YES}:do"),
               B("❌ Отмена", None, f"{CBT.HIST}:1"))
        edit(c.message,
             f"⚠️ <b>Очистить историю?</b>\n\n"
             f"Будет удалено <b>{finished_count}</b> завершённых/отменённых заказов.\n"
             f"<i>Активные аренды не затрагиваются.</i>",
             kb)

    def history_clear_do(c):
        answer(c)
        with _data_lock:
            to_delete = [k for k, o in ORDERS.items()
                         if o.status in (RentStatus.FINISHED, RentStatus.REFUND, RentStatus.ERROR)]
            count = len(to_delete)
            for k in to_delete:
                del ORDERS[k]
            _save_orders()
        with _processed_lock:
            for k in to_delete:
                _processed_orders.pop(k, None)
        logger.info(f"[ASRplus] История очищена: удалено {count} записей")
        kb = K(row_width=1)
        kb.add(B("⬅️ Назад", None, f"{CBT.HIST}:1"))
        edit(c.message, f"✅ <b>История очищена</b>\n\nУдалено записей: <b>{count}</b>", kb)

    def open_history_detail(c):
        oid = _p(c)
        txt, _ = _order_detail_text(oid)
        edit(c.message, txt, _back_kb(f"{CBT.HIST}:1"))

    def get_files_confirm(c):
        kb = K(row_width=2)
        kb.add(B("✅ Да, отправить", None, CBT.FILES_CONFIRM), B("❌ Отмена", None, CBT.MAIN))
        edit(c.message, "⚠️ <b>Файлы содержат пароли и секреты Steam!</b>\n\nОтправить в чат?", kb)
        answer(c)

    def get_files(c):
        answer(c)
        chat_id = c.message.chat.id
        now_str = _fmt(_now())
        SEP = "=" * 40

        def _send_txt(fname, content, caption):
            _send_txt_file(chat_id, fname, content, caption)

        # ── 1. АККАУНТЫ ──────────────────────────────────────────────────────
        try:
            with _data_lock:
                accs_snapshot = list(ACCOUNTS)

            STATUS_LABEL = {
                RentStatus.FREE:     "[СВОБОДЕН]",
                RentStatus.ACTIVE:   "[АРЕНДОВАН]",
                RentStatus.BUSY:     "[ЗАНЯТ]",
                RentStatus.ERROR:    "[ОШИБКА]",
                RentStatus.FINISHED: "[ЗАВЕРШЁН]",
                RentStatus.REFUND:   "[ВОЗВРАТ]",
            }
            lines = [
                f"ASRplus — АККАУНТЫ",
                f"Экспорт: {now_str}",
                f"Всего:   {len(accs_snapshot)} шт.",
                SEP,
            ]
            for a in accs_snapshot:
                st = STATUS_LABEL.get(a.status, a.status)
                lines += [
                    f"",
                    f"  #{a.id}  {a.login}",
                    f"  {'─' * 36}",
                    f"  Пароль   : {a.password}",
                    f"  Тег      : {a.tag}",
                    f"  Статус   : {st}",
                ]
                if a.status == RentStatus.ACTIVE:
                    lines += [
                        f"  Арендатор: {a.owner or '—'}",
                        f"  До       : {a.rental_end or '—'}",
                    ]
                if a.access_count:
                    lines.append(f"  Выдач    : {a.access_count}")
                if a.time_limit_hours is not None:
                    lines.append(f"  Лимит ч. : {a.time_limit_hours}")
            lines += ["", SEP]
            _send_txt(
                "accounts.txt",
                "\n".join(lines),
                f"📂 <b>accounts.txt</b>\n∟ Аккаунты + пароли\n∟ {len(accs_snapshot)} шт.  |  {now_str}"
            )
        except Exception as e:
            logger.warning(f"[ASRplus] Ошибка формирования accounts.txt: {e}")
            bot.send_message(chat_id, f"❌ Не удалось сформировать accounts.txt: {_safe_err(e)}", parse_mode="HTML")

        # ── 2. НАСТРОЙКИ ─────────────────────────────────────────────────────
        try:
            s = SETTINGS
            on = lambda v: "ВКЛ" if v else "ВЫКЛ"

            lots_lines = []
            for lid in s.lots:
                lc = s.get_lot(lid)
                tag_str = lc.tag if lc else "?"
                ext_str = f"  -> продление #{lc.extend_lot_id}" if lc and lc.extend_lot_id else ""
                lots_lines.append(f"    #{lid:<12} тег: {tag_str}{ext_str}")

            rev_lines = [
                f"    >= {r['rent_hours']} ч  ->  +{r['bonus_hours']} ч бонус"
                for r in s.review_rules
            ]

            bl_lines = [f"    {u}" for u in s.blacklist] or ["    (пусто)"]

            lines = [
                f"ASR+ v{VERSION} by @DzhantDev — НАСТРОЙКИ",
                f"Экспорт: {now_str}",
                SEP,
                f"",
                f"  [ОСНОВНЫЕ]",
                f"  Плагин включён       : {on(s.enabled)}",
                f"  Автовозврат/ошибка   : {on(s.autoback_on_error)}",
                f"  Авто-продление       : {on(s.auto_extend)}",
                f"  Авто-откл. лоты      : {on(s.auto_disable_lots)}",
                f"  Авто-вкл. лоты       : {on(s.auto_enable_lots)}",
                f"  Авто-свободен/ошибка : {on(s.auto_free_on_error)}",
                f"  Сохр. удалённых      : {on(s.save_deleted_acc)}",
                f"",
                f"  [УВЕДОМЛЕНИЯ]",
                f"  Заказ выдан  : {on(s.notification_order_completed)}",
                f"  Ошибки       : {on(s.notification_error)}",
                f"  Возвраты     : {on(s.notification_refund)}",
                f"",
                f"  [ЛОТЫ — {len(s.lots)} шт.]",
            ] + (lots_lines if lots_lines else ["    (пусто)"]) + [
                f"",
                f"  [БОНУСЫ ЗА ОТЗЫВ]",
            ] + rev_lines + [
                f"",
                f"  [ЧЁРНЫЙ СПИСОК — {len(s.blacklist)} шт.]",
            ] + bl_lines + [
                f"",
                SEP,
            ]
            _send_txt(
                "settings.txt",
                "\n".join(lines),
                f"⚙️ <b>settings.txt</b>\n∟ Настройки плагина\n∟ {now_str}"
            )
        except Exception as e:
            logger.warning(f"[ASRplus] Ошибка формирования settings.txt: {e}")
            bot.send_message(chat_id, f"❌ Не удалось сформировать settings.txt: {_safe_err(e)}", parse_mode="HTML")

        # ── 3. ИСТОРИЯ ЗАКАЗОВ ───────────────────────────────────────────────
        try:
            with _data_lock:
                orders_snapshot = sorted(ORDERS.values(), key=lambda o: o.created_at, reverse=True)

            STATUS_LABEL_O = {
                RentStatus.ACTIVE:   "[АКТИВЕН]",
                RentStatus.FINISHED: "[ЗАВЕРШЁН]",
                RentStatus.REFUND:   "[ВОЗВРАТ]",
                RentStatus.ERROR:    "[ОШИБКА]",
            }
            lines = [
                f"ASR+ v{VERSION} by @DzhantDev — ИСТОРИЯ ЗАКАЗОВ",
                f"Экспорт: {now_str}",
                f"Всего:   {len(orders_snapshot)} шт.",
                SEP,
            ]
            for o in orders_snapshot:
                st  = STATUS_LABEL_O.get(o.status, f"[{o.status}]")
                ext = "  [ПРОДЛЕНИЕ]" if o.is_extension else ""
                lines += [
                    f"",
                    f"  Заказ    : {o.id}{ext}",
                    f"  {'─' * 36}",
                    f"  Покупатель : {o.buyer}  (id: {o.buyer_id})",
                    f"  Аккаунт    : {o.acc_login}  (#{o.acc_id})",
                    f"  Тег        : {o.acc_tag}",
                    f"  Часов      : {o.hours}",
                    f"  Статус     : {st}",
                    f"  Создан     : {o.created_at}",
                ]
                if o.lot_id:
                    lines.append(f"  Лот        : #{o.lot_id}")
            lines += ["", SEP]
            _send_txt(
                "orders.txt",
                "\n".join(lines),
                f"📜 <b>orders.txt</b>\n∟ История заказов\n∟ {len(orders_snapshot)} шт.  |  {now_str}"
            )
        except Exception as e:
            logger.warning(f"[ASRplus] Ошибка формирования orders.txt: {e}")
            bot.send_message(chat_id, f"❌ Не удалось сформировать orders.txt: {_safe_err(e)}", parse_mode="HTML")

        # ── 4. БЭКАПЫ ПАРОЛЕЙ ────────────────────────────────────────────────
        try:
            with _pwd_backup_lock:
                bk_snapshot = {k: v for k, v in PWD_BACKUPS.items() if v.get("bot") or v.get("human")}
            lines = [
                f"ASRplus — БЭКАПЫ ПАРОЛЕЙ",
                f"Экспорт: {now_str}",
                f"Аккаунтов: {len(bk_snapshot)} шт.",
                SEP,
            ]
            for acc_id, data in sorted(bk_snapshot.items(), key=lambda kv: (kv[1].get("login") or "").lower()):
                lines.append("")
                lines.append(_pwd_backup_txt_block(acc_id, data))
            lines += ["", SEP]
            _send_txt(
                "pwd_backups.txt",
                "\n".join(lines),
                f"💾 <b>pwd_backups.txt</b>\n∟ Бэкапы паролей\n∟ {len(bk_snapshot)} аккаунтов  |  {now_str}"
            )
        except Exception as e:
            logger.warning(f"[ASRplus] Ошибка формирования pwd_backups.txt: {e}")
            bot.send_message(chat_id, f"❌ Не удалось сформировать pwd_backups.txt: {_safe_err(e)}", parse_mode="HTML")

    
    tg.cbq_handler(open_main, lambda c: c.data == CBT.MAIN or c.data.startswith(CBT.SP))
    tg.cbq_handler(open_about, lambda c: c.data == CBT.ABOUT or c.data == f"{CBT.ABOUT}:howto")
    tg.cbq_handler(open_config, lambda c: c.data == CBT.CONFIG)
    tg.cbq_handler(open_functions, lambda c: c.data == CBT.FUNCTIONS)
    tg.cbq_handler(open_pwd_backups, lambda c: c.data == CBT.PWD_BACKUPS)
    tg.cbq_handler(pwd_backup_download_all, lambda c: c.data == CBT.PWD_BACKUP_DL_ALL)
    tg.cbq_handler(open_blacklist, lambda c: c.data == CBT.BLACKLIST)
    tg.cbq_handler(blacklist_add_start, lambda c: c.data == CBT.BLACKLIST_ADD)
    tg.cbq_handler(open_acc_menu, lambda c: c.data == CBT.ACC_MENU)
    tg.cbq_handler(open_bulk_accs, lambda c: c.data == CBT.BULK_ACCS)
    tg.cbq_handler(bulk_upload_start, lambda c: c.data == CBT.BULK_UPLOAD)
    tg.cbq_handler(bulk_download, lambda c: c.data == CBT.BULK_DOWNLOAD)
    tg.cbq_handler(open_acc_by_tag, lambda c: c.data == CBT.ACC_BY_TAG or c.data.startswith(f"{CBT.ACC_BY_TAG}:"))
    tg.cbq_handler(acc_search_start, lambda c: c.data == CBT.ACC_SEARCH)
    tg.cbq_handler(start_add, lambda c: c.data == CBT.ACC_ADD)
    tg.cbq_handler(open_lots, lambda c: c.data == CBT.LOTS)
    tg.cbq_handler(open_lots_hourly, lambda c: c.data == CBT.LOTS_HOURLY)
    tg.cbq_handler(open_lots_fixed, lambda c: c.data == CBT.LOTS_FIXED)
    tg.cbq_handler(open_tag_extend_list, lambda c: c.data == CBT.TAG_EXTEND)
    tg.cbq_handler(lot_add, lambda c: c.data.split(":")[0] == CBT.LOT_ADD)
    tg.cbq_handler(lots_disable_all, lambda c: c.data == CBT.LOTS_DISABLE_ALL)
    tg.cbq_handler(lots_enable_all, lambda c: c.data == CBT.LOTS_ENABLE_ALL)
    tg.cbq_handler(open_reviews, lambda c: c.data == CBT.REVS)
    tg.cbq_handler(rev_add, lambda c: c.data == CBT.REV_ADD)
    tg.cbq_handler(open_notifs, lambda c: c.data == CBT.NOTIFS)
    tg.cbq_handler(open_msgs, lambda c: c.data == CBT.MSGS)
    tg.cbq_handler(open_stats, lambda c: c.data == CBT.STATS)
    tg.cbq_handler(open_full_stats, lambda c: c.data == CBT.FULL_STATS)
    tg.cbq_handler(open_top_tags, lambda c: c.data == CBT.TOP_TAGS)
    tg.cbq_handler(open_active_rents, lambda c: c.data == CBT.ACTIVE_RENTS)
    tg.cbq_handler(open_pending_orders, lambda c: c.data == CBT.PENDING_ORDERS)
    tg.cbq_handler(open_free_accs, lambda c: c.data == CBT.FREE_ACCS)
    for pfx, handler in [
        (CBT.ACC_LIST, open_acc_list), (CBT.ACC_DETAIL, open_acc_detail),
        (CBT.ACC_CODE, acc_code), (CBT.ACC_STOP, acc_stop),
        (CBT.ACC_CHPWD, acc_chpwd), (CBT.ACC_EXTEND_DO, acc_extend_do),
        (CBT.ACC_RESET, acc_reset),
        (CBT.ACC_MANUAL, acc_manual_start), (CBT.ACC_MANUAL_HOURS, lambda c: None),
        (CBT.ACC_DEL_CONFIRM, acc_del_confirm), (CBT.ACC_DEL_YES, acc_del_yes),
        (CBT.ACC_DEL_NO, acc_del_no),
        (CBT.LOT_DETAIL, open_lot_detail), (CBT.LOT_EDIT, lot_edit),
        (CBT.LOT_EDIT_TAG, lot_edit_tag),
        (CBT.LOT_RENAME, lot_rename),
        (CBT.LOT_NOTE, lot_note),
        (CBT.LOT_FIXED_HOURS, lot_fixed_hours),
        (CBT.LOT_DEL_CONFIRM, lot_del_confirm), (CBT.LOT_DEL_YES, lot_del_yes),
        (CBT.LOT_DEL_NO, lot_del_no), (CBT.LOT_TOGGLE_FP, lot_toggle_fp),
        (CBT.LOT_TAG, lot_tag),
        (CBT.LOT_AUTO_TAG, lot_auto_tag),
        (CBT.REV_HRS, rev_hours), (CBT.REV_BON, rev_bonus),
        (CBT.REV_DEL, rev_del), (CBT.MSG_EDIT, msg_edit),
        (CBT.MSG_RESET, msg_reset),
        (CBT.TOGGLE, toggle_setting), (CBT.HIST, open_history),
        (CBT.HIST_DETAIL, open_history_detail),
        (CBT.HIST_CLEAR, history_clear_confirm), (CBT.HIST_CLEAR_YES, history_clear_do),
        (CBT.HIST_CLEAR_NO, open_history),
        (CBT.ACC_SET_PWD, acc_set_pwd), (CBT.ACC_EDIT_MAFILE, acc_edit_mafile),
        (CBT.BLACKLIST_DEL, blacklist_del),
        (CBT.ACC_SET_LIMIT, acc_set_limit_start),
        (CBT.BULK_CONFIRM, bulk_confirm),
        (CBT.LOT_EXTEND_LOT, lot_extend_lot_menu),
        (CBT.LOT_EXTEND_LOT_SET, lot_extend_lot_set),
        (CBT.LOT_EXTEND_LOT_DEL, lot_extend_lot_del),
        (CBT.LOT_EXTOPT_ADD, lot_extopt_add),
        (CBT.LOT_EXTOPT_DEL, lot_extopt_del),
        (CBT.TAG_EXTEND_DETAIL, open_tag_extend_detail),
        (CBT.TAG_EXTEND_LOT_SET, tag_extend_lot_set),
        (CBT.TAG_EXTEND_LOT_DEL, tag_extend_lot_del),
        (CBT.TAG_EXTOPT_ADD, tag_extopt_add),
        (CBT.TAG_EXTOPT_DEL, tag_extopt_del),
        (CBT.PWD_BACKUP_ACC, open_pwd_backup_detail),
        (CBT.PWD_BACKUP_DL_ACC, pwd_backup_download_acc),
    ]:
        tg.cbq_handler(handler, lambda c, p=pfx: c.data.startswith(f"{p}:"))
    tg.cbq_handler(acc_extend_menu, lambda c: c.data.startswith(f"{CBT.ACC_EXTEND}:") and c.data.count(":") == 1)
    tg.cbq_handler(get_files_confirm, lambda c: c.data.startswith(f"{CBT.FILES}:"))
    tg.cbq_handler(get_files, lambda c: c.data == CBT.FILES_CONFIRM)

    for state, handler in [
        (States.LOGIN, _h_login), (States.PASS, _h_pass),
        (States.TAG, _h_tag), (States.MAN_BUYER, _h_manual_buyer),
        (States.MAN_HOURS, _h_manual_hours),
        (States.LOT_ID, _h_lot_id), (States.MSG_EDIT, _h_msg_edit),
        (States.SET_PWD, _h_set_pwd), (States.LOT_RENAME, _h_lot_rename),
        (States.LOT_NOTE, _h_lot_note),
        (States.LOT_FIXED_HOURS, _h_lot_fixed_hours),
        (States.LOT_FIXED_HOURS_ADD, _h_lot_fixed_hours_add),
        (States.REV_HRS_CUSTOM, _h_rev_hrs_custom),
        (States.REV_BON_CUSTOM, _h_rev_bon_custom),
        (States.EXTEND_LOT_ID, _h_extend_lot_id),
        (States.EXTOPT_LOT_ID, _h_extopt_lot_id),
        (States.EXTOPT_HOURS, _h_extopt_hours),
        (States.TAG_EXTEND_LOT_ID, _h_tag_extend_lot_id),
        (States.TAG_EXTOPT_LOT_ID, _h_tag_extopt_lot_id),
        (States.TAG_EXTOPT_HOURS, _h_tag_extopt_hours),
    ]:
        tg.msg_handler(handler, func=lambda m, s=state: tg.check_state(m.chat.id, m.from_user.id, s))
    tg.msg_handler(_h_mafile, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, States.MAFILE))
    tg.msg_handler(_h_mafile_edit, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, States.EDIT_MAFILE))
    tg.msg_handler(_h_acc_search, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, States.ACC_SEARCH))
    tg.msg_handler(_h_blacklist_add, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, States.BLACKLIST_ADD))
    tg.msg_handler(_h_set_limit, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, States.SET_LIMIT))
    tg.msg_handler(_h_bulk_mafile, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, States.BULK_MAFILE))
    try:
        tg.file_handler(States.MAFILE, _h_mafile)
        tg.file_handler(States.EDIT_MAFILE, _h_mafile_edit)
        tg.file_handler(States.BULK_MAFILE, _h_bulk_mafile)
    except Exception:
        pass
    tg.msg_handler(open_main_cmd, commands=['asrplus'])
    tg.msg_handler(cmd_free, commands=['free'])
    tg.msg_handler(cmd_busy, commands=['busy'])
    card.add_telegram_commands(UUID, [
        ("asrplus", "открыть настройки ASRplus", True),
        ("free", "свободные аккаунты", True),
        ("busy", "занятые аккаунты", True),
    ])

    threading.Thread(target=rental_check_loop, args=(card,), daemon=True).start()
    _ensure_order_worker(card)
    threading.Thread(target=_worker_watchdog, args=(card,), daemon=True, name="ASRplus-Watchdog").start()
    logger.info("[ASRplus] Worker + Watchdog запущены при старте")

    # Сообщение об успешной установке
    try:
        for uid in tg.authorized_users:
            bot.send_message(
                uid,
                "╔══════════════════════╗\n"
                "║   <b>⚡ ASR+ v1.2.0</b>         ║\n"
                "╚══════════════════════╝\n\n"
                "✅ Плагин успешно загружен и готов к работе!\n\n"
                "👤 Разработчик: <b>@DzhantDev</b>\n"
                "📢 Канал: <a href=\"https://t.me/DzhantDev\">t.me/DzhantDev</a>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "<i>© ASR+ by @DzhantDev — автоматическая аренда Steam</i>",
                parse_mode="HTML",
                disable_web_page_preview=True
            )
    except Exception:
        pass

def cleanup(card: Cardinal):
    _stop_event.set()

BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [process_new_order]
BIND_TO_NEW_MESSAGE = [process_message]
BIND_TO_ORDER_STATUS_CHANGED = [process_order_status_changed]
BIND_TO_DELETE = cleanup