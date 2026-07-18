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

ICON_STATUS = {"FREE": "🟢", "ACTIVE": "👤", "BUSY": "⏳", "ERROR": "❌", "FROZEN": "⏸"}

CODE_COOLDOWN = 5.0
SELLER_CALL_COOLDOWN = 60.0
PASSWORD_CHANGE_TIMEOUT = 180
ONE_STAR_REFUND_WINDOW_SECONDS = 900

class SteamEmailVerificationRequired(Exception):
    pass

FUNPAY_LOT_URL = "https://funpay.com/lots/offer?id={lot_id}"

# Логотип ASR+ (встроен как base64, чтобы плагин оставался одним файлом)
ASR_LOGO_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD//gAQTGF2YzYwLjMxLjEwMgD/2wBDAAYEBAUEBAYFBQUGBgYHCQ4JCQgICRINDQoOFRIWFhUSFBQXGiEcFxgfGRQUHScd"
    "HyIjJSUlFhwpLCgkKyEkJST/2wBDAQYGBgkICREJCREkGBQYJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCT/wAAR"
    "CAKAAoADASIAAhEBAxEB/8QAHQABAQACAwEBAQAAAAAAAAAAAAEHCAQFBgIDCf/EAFoQAAEDAwEEBwQFBwYIDQIHAAABAgMEBREGBxIhMQgTQVFhcYEUIpGh"
    "FSMyQmIWUnKCkrHBJDNDk7LRFzREdaLC0vAYJSc2N0VGVmRzlLPhNVVTVGODo8Px/8QAGwEBAAIDAQEAAAAAAAAAAAAAAAQFAQMGAgf/xAA8EQEAAQMBBQUG"
    "BQMDAwUAAAAAAQIDBBEFEiExQQYTUWFxFCKBkaHBMrHR4fAjM0IVFlI0YvEkNUNygv/aAAwDAQACEQMRAD8A2oAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAA"
    "qgQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAYAAAAAAAAAAAYKgEBVIAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAuCF7AIBgYAAAABgAAAAAAAAAAAAwAAAAAAqkAAAC5IMDAADAAAAAAAAAQBgFIAAAAAAAMAAAMAAUigAXsIALjgQAAAACAAAgAFAyMhhAVVIGQAAAUKoEAA"
    "AAuAIBgYAAFRACIQqgCAuQBAXIRQICqpAAAAZKQAXAIAKMEAAZAAAAC4IAACAAXBC5HMCFCqQC8xggAuBgZIBQpABckKMgRCkAFC8iACogwQAUilyRQAAADI"
    "QoBSAAC5GCAUigKAAAAAAC9hAAKhCoAXAQigCqEIAAAAAAAVSAAXgQACoQAAMgAUZIBQpMgCoB2DIAgAAAAFAAAAAAAAAAF4EAAAAAAAAAAFQgAAAAAAAAAB"
    "QAL2EUZAFUEAFJ2gAXgFIAAGAAAAAAuAIEAAoUgAAACkAAAAAAAKhBkAAMgAAAAAAAAAAAAAQAXgQqIBAVUGOAEAAAAAXJAAAA4gAAAAAAAAAAAAAADiAAAA"
    "AAAAAAUAAAAAAAAAAAAABUQCAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAqEAAAC54EAAAAACgCBAAAAAFRSACqpAAAAAFwQqAQBSoBAAAAAAAAAAAAAAAqAQ"
    "AAAAAAAAAAAAAAAAAAAAAAAAqKQAVVIAAAAAAAAAAAAAAAAAAAAFRQoQKBAAAAAAYBewAgCACAAAMgqAEGAFAKQAAAAGQAAAwMAAUYAgAAFQheADAGQAGAEA"
    "mAVSAAAAAAAAAMjIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAACqROZVAgAAAAAMgABkFUCAAAMgqAEAGQIXBMZKAAAAhRyAAAAAAIAA"
    "KiEBQIC44gB2DICAMEKAIAAAAAAFyBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGAAAAAAAAVAIC5IoAAAVCAACkLkBghQAAUmAKTtGABfIAB"
    "jUCjJMhkyVSYADJUJgAMjIAAAACopABQQvMACACooVSAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMgC9hC9gEAAAAACkADtCgAAAAA"
    "AAqEKigRSopFCAUABgHAmQAAAZAAAHwAUAAAABccAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAAAAAAAAA"
    "AAAAAqKAgQCdpUAAKpCqQAAAHAAACqpAABQqAQAAC54EAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAUKAGELgIAyAAApEUqkAAAC5IAAAAApAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABgABgAAMAAAAAAAAAAAAAwCgQBQAAAAAAAAALggAAqkA"
    "ABAKFAUCAAAAAKAAAGSAUKQAMgAAAAAAAADIAHUXXV+n7Ii/SV5oKVU5tkmajvhnJ5O4bedDUWUjuE9Y5Oynp3L81whrqu0U85SrODkXv7duZ9IlkMGIKnpJ"
    "2Fqr7LZ7nN3K9WM/ip1dR0l3ZX2fTSY7Osq/7mmqcu1H+Swo7O7Rr4xan6R92cwa91HSVvSZWKwW9qfimev9xmDQF7u2pdL0d3u9JBST1aLIyKFXYSPPuque"
    "1U4+p6t5FFydKWjO2PlYVEV5FOmvnD0gOPcK2nttFPW1UiR09PG6WR68mtamVX5GBYelczrnddpZ6w7y7ro6v3lb2KqK3mbK7lNH4kfGwb+TEzZp10bBAwzQ"
    "9KPSc2PbLXd6VV5q1jJET4ORfkelt23rZ7cVRPp5tK5eyqifH81THzMReonq9XNm5VH4rc/JkEHX2zUNnvUaSW26UVY1eSwTNf8AuU7DJ7iYnkhzTNM6TAAD"
    "LAAABccCFyARCBQAAAAAAAAAALngBAhUUgAuSAC5HMhQBO0pO0C4GBknNQAQuAoDBAAAAADIAAAAAAAAAAZAAAAC5IAAUAAXsIAAAAAAAVSFUgAAAAAAAGQB"
    "FU6HU2ubBpKFX3a4RQvxlsLV3pX+TU4mINT9Iqtqd+DTtvZSMXglRVe/J5oxOCeuTTcyLdv8UrPB2Pl5k/0aOHjPCGeKqsp6KF09VPFBC3i6SVyNanqpj/UG"
    "3fSNlV8dNPNdJm/dpGZZ+2uE+GTXa86kvGoZ1mu1yqax+cokr1VrfJvJPRDrMEC5tCZ/BDsMPsZbpjeyq9Z8I4R8/wDwyte+kVqOtc5lqoaO3Rrye9Otk+eE"
    "+Snh7vrrVF+VfpC+V0zF/o0kVjP2W4Q/HS+kbxrGv9itFMsrm4WSR3COJF7XO7PLmZp090eLPSxNkvdwqq2bHvMgXqo0X5qvxQ1URfv8deCdfubI2TO7uxve"
    "ERrP15NfnZVyuXiq81IqeBtDJsP0LLGrG2+djvzmVT95PiqnjdTdHVGxvm05dHOcmcU1Z2+CPTl6oYrwrkRrze8btbgXKt2rWn1jh9NWEORTsL1Ybnp2tdRX"
    "WjmpJ2/dkTg5O9q8lTxQ69eBEmJidJdLRcorpiuidYnq9DoXSztYapobTur1L39ZUOT7sTeLvjy9Tb2CGOngZDExGRxtRrWpyaiJhEMS9HnS/sVjqNQzx7s1"
    "evVwqqcoWrz9XfuQy6qo1qqqoiInFV7C6wrW5RrPOXyvtRtD2rLm3TPu0cPj1/T4MN9JbWS2jSkOn6eTFTdn4kwvFsDcK74rhPiav+h6/azq9dba4uFxjerq"
    "SJ3s1InZ1TFxlP0ly71OmtGmLjerTeLpSRK6ntETJZ1x2Odjh5JlV8EI96qblc6Og2Vj04WLT3nCZ4z6zydSgO20o20SaioIr8x77ZNKkU6skVjmI7gj0VO5"
    "VRfIz5c+izYqhqutV+uNKq8mzNbM3+C/M8UWqq41pbsvadnFrii7rGvXTg1uikfBIkkL3xPbycxytVPVD19i2u6309uto9Q1kkbf6KqVJm/6eV+Cnrr10ZNX"
    "UCOfbay23Rqcmo9YXr6O4fMx3f8ARWpNLuVLxZK+jan9I6JVjX9ZMp8xNFygoyMLLjTWmryn92XtPdKevh3Y9QWKGobyWaierHee67KfNDKWmNteiNUuZDT3"
    "iOkqX8oK1OpdnuRV91fRTTRHI5MoqKgVEdzTKGynKrjnxQsns9i3eNv3Z8n9BGuR7Uc1yOaqZRUXgp9GkGltpeq9Gub9E3idsCf5NMvWQr+qvL0wZp0j0obb"
    "VLHT6ptslBIvBaqlzJEq96t+030ySqMqirnwc7l7AybPGj3o8ufyZ2BwLPf7XqGibW2mup62mfykhejk9e5fBTnkmJ15KSqmaZ0kAAYAAAAAAAAAAAAAAAAC"
    "oEUAOJCgAgADAAFDKAAABgAAAAAAAAAAAAAAAAAAAAAAFHAIoAgAAADIFUmCgCAuCAAq45g87tD1Muj9I194Yxr5oWI2JjuSyOVGtz4ZXPoYqmKY1lstW6rt"
    "cW6OczpDlaj1ZZdK0a1V2r4qZi/YbzfIvc1qcVMIaz293W6dZS6fYttpV4de7CzvT9zfTK+Jiy6Xm432vkuFzq5auqkX3pJFzjwTuTwQ46Kq9pT386qrhRwh"
    "9L2V2Ux8fSvI9+r6R+vx+T9J55aqd888r5pXrl0kjlc5y+KqfBMAgz4usimKY0hTl2q11N5uNNbqOPrKipkSONviv8O0/Cjpai4VUdJSU8tRUSLhkUTVc53o"
    "bE7Jtlv5HRrer0ka3SRmGMyitpWY48eW93r2G/HsTcq8lNtjbFrBtTOutc8o/nR3sMNk2P6HV7kzHTNTfc1PrKqZf4qvwTyMA6r2p6n1bUPWauloqNVXcpKZ"
    "6sa1PxKnFy+fwO7206/i1Zd47bbZd+229Vw9q8J5V4K5PBE4J69557ZtpSn1lqymtVXM6Knc18sm4uHPRqZ3U8VJF67NVcWrfJSbJ2dbxsaraGdGtc61ceOk"
    "enjLoYbhW08iSxVtVFInHeZK5FT1RT3elNt2pbBIyOvmW8UScHMnX61qfhfz+OT0O1rZRYtL6aW8WZJqd8MrGSRvlV7ZGuXHbyVFPI6P2RX/AFhaFutNLS01"
    "M5yti69XIsuOaphFwmeGVNe5et17tPNMqzdl5uL31+IinXTjHHXy0+zJO0LaDpHU+zeeoi6mrqpnJFBTzJianlXjvKnNMIi8U4KYQ07YajU98obPS536uVGK"
    "5PuN5ud6IiqcWuo5bfXVFFOjUmgkdFJurlEc1cLx7TNvR30nuR1mp6iPi/8AktKqp2ffcnyT0UzG9kXYiqHmumzsXZ9yuzVM734dfGeXy5+bMlrtsFooKegp"
    "WIynpo2xRtTsaiYQ8Ft31p+SOhamKCXcr7nmkgwvFqKnvuTybn1VDI6GrfSguE8+vKKie9eopqBro2diOe528vmu6nwLS/VuUcHBbJsRk5dMV8uc/Bh3CNbw"
    "5IbebHtAw2DZrFQ3CnR092Y6orGOTmkjcIxfJuE88muuyTSP5Z66t9vkYrqSJ3tNVw4dWzjhfNcJ6m6SNTCInBO5DRi0c6lz2lzONOPTPnP2aKay0zNpHU9y"
    "sVRn+Syq1jl+/GvFjvVqobW7D9Xpq3QVG+aXrK2g/klTleKq1Pdcvm3C/E8D0odFpJS0OraWL34VSkq1RPuKuWOXyXKfrIYCt14uVpk623XCronrxV1PM6NV"
    "88KeN6bNc+CVNmNrYVE66VR+fX582/XAjmMkarHNRzV4KiplFNObNt12gWVzUbe1ro28462NJM/rcHfMyNp7pUtVWR6i0+rc/amoZM4/Ud/eSKcmieajv9n8"
    "u3xpiKvRkvUmxrROqN99XY4Ked3H2ij+pkz3+7wX1RTE2qei3cabfm0zd4qtnNKatTq3+SPTgvqiGYtM7V9HasVrLdeqdKhyf4vUL1Unwdz9MnrU4oeptW7n"
    "FotbQzcOrd1mPKf3aIai0nfdJ1HUXy1VVA5Vw10jPcf5PT3V9FOpN/a620dzpX0tbTQ1NO9MOimYj2u80UxHrPo06evCSVGnZnWWqXj1WFfTuXy5t9OHgRq8"
    "SY40ugw+0tur3ciNPOOX6tcLDqS76XrErbLcaihqE5uidhHeDk5OTzQzxoLpNQVHV0OsadKaTklwp25jXxezm3zTKeCGF9Y7PtRaEqupvdvfFE5cMqY/ehk8"
    "nJ+5cKedyaKbldudFrkYWLn0b3CfOG/tvuNHdaOKsoaqGqppU3o5Yno5rk8FQ5JpRs42lXfZ5dopqWd8ltkkT2qicuWPbniqJ2OROSp6m6kEzKiGOaN28yRq"
    "PaveiplCxs3ouQ4jaWza8KuImdYnlL7ABuVoAAAAAAAAAAAAAF4kTmUAAAwAAGopC4IgZAXAwBCoAGNQAZAgQAMigFVAIC44EAF4EAAAAAAAAAAqqQAAAAAA"
    "FQBAAACgTtMX9Imo6vQUUSL/AD1dE1fRHO/gZQMRdJKTGlrXH+fXZ+Ebv7zRkzpaqWuw6d7Psx/3Q147SkQzTsO2aw1rWaqu8KSRNcqUML0y1VTgsqp28eCe"
    "WSks2puVbsPq209pW8GxN658I8ZdJonYbetRwMrrrItoo3pljXM3pnp37v3U8/gZHoOj9pKkRrqiS41ipz6yZGIvo1EOk2nbbpbXWTWTTDo1niVWT1rkRyRu"
    "7WsTkqp2qvBDDdw1Vfrq9zq683CoV3NHzux8EXBLqqsWvdiNZcxZx9sbSjvqrvd0zyiPD8/nLZSWv2fbMqZ/VOttBJjjHBiSeTw7XL6mG9om2S5avR9utrJL"
    "daV4ObvfWzp+JU5J4J6mOlVVXKrlV5r2kNV3Lqqjdp4QtNn9mbGPX316qblfjP8APzVOzwO70lQaiqrtHPpmCrkrqb6xH06ZWPxXswvLjzOkMv7Cdc2DTdJc"
    "rdeKmKhkmlbMyaXg16buN3PZjn6mqxTFVcRM6J+2L9yxi1V2qN+eWnPhPPg8drjWOrr65tp1LJJC6ldvOpliSL3uxzkTn4Ho9I7Zl01o11jdRPfWU7HspZmO"
    "Td45VN9OfBV7OZ8bT9Sae1tru1so59+jjSOmqKtE3UeiyccKvYiLzOLtofZKa+0tjsVJSwQW6LEjoWpl0jscFd24THqqkiqZomquKtdOCmt2rOTbsYt2xu70"
    "TVpHCKdP18PN4Sjoqy9XOCkgRZausmSNq81c9y81+OTcfTVjg05YqG0UyJ1dLE2PP5y9rvVcr6mDej3pBa+9VGo6liLBQosNPlOcrk4r6N/tGwicCVg29Kd/"
    "xc/2tzu8vxjUzwo5+s/pCmqXSYTG0aNe+3xf2nmY9pe2Oj0W91st0bK674y5rl+rp88t/HNfwms2tb/d9W3xLhdJ3VVW9iMajWIm61FXDWtTs4mcm9RPuRze"
    "dgbLyKaozK40o04a9dfszj0XtMpSafuOopWYlr5uoiVU49VHzx5uVf2TOScjSHT20nWGj0igtl4q6eCBVVKST3okyuVRWOTh8jYjZPtzo9dyttF1iioL1u5Y"
    "1q/VVKJzVmeTvwr6Gyxdo0ilF2zszIi5Vkzxpnw6QyHqaxU2p7DX2arRFgrYHQuX81VTgvmi4X0NFbnbKmyXOrtdazcqqOZ0EqfiauM+vM38zlDWXpM6L+jL"
    "9S6opo8QXJOpqcJwSZqcF/Wb/ZMZVGsbz12cy+7vTZq5VfmwrgqEKVzuUXjwXkev0ltZ1jox7G2+7ST0jedJWfWxKncmeLfRUPIA9U1TTxhpvY9u9Tu3KYmG"
    "0ei+knp29dXTagidZKt3DrFXfp3L+lzb6pjxMuU1ZTVsDJ6WeOeGRMskjcjmuTvRUNAD0+g9eag0Zd6VbVcJI6aSdjZqV670UjVciL7q8EXHamFJdvKnlU5n"
    "P7OUaTXjzp5TybrVtDTXCnfTVdPFUQSJh8crUc1yeKKarbf9n1n0PerfNZIHU1PcI5Hvg3ssje1U+xniiLnkbYN4oa/9K+NOo03Ljjv1DM+jFN2TTE0TKn2D"
    "eroy6aYnhOuvya8OyrXInahvVoKpWs0TYahy5WS3wKv7CGixu1sll63Zppp3/gIk+CYNOHzlc9p4/p0T5vWgAnuNAAAAAAAAAAACAIBcdoAAAAMaAJkoNAAB"
    "kAADJFAAAAAAAAAAFIAKqkAAAAAAAAAAAAAAABewhUAAAATJQoEMOdJVcWCyp31j/wD21MxmHOks1V0/ZXd1a5P/AONSPlf2qlvsD/3C16sCUlM6rqoaZnF8"
    "0jY2+arj+JtNrm4s2fbM52UGI301Oyjpsdjlw1F804qaz6XcjNS2hzuSVsGf20M+9IdHO0HHjOEr4ld5Yd/Er8Wd23XVHN2HaGO+zsWxX+GZ+8NblXmq5Vea"
    "qvNTKFFsFvdXpf6WWthZWvhSeKi3c7yYyiK/PByp4GMG9htbslrrzX6Mo/pqilp5YUSKJ8iYWaJETdfjmnDhx54ya8S1TcqmKoTe0m0MjCtUV49URx4/zw8W"
    "rtstNwvVYlFbqKeqqVzmKJmXNxzz3ep8V9vq7XWS0ddTyU1TEuHxSJhzVNxIbZZdNR1tdFTUdvjlcs9VMjUYjl7XOU1t2wattOr9UMqbOxXQ08PUOqFTHXrn"
    "OUTuTkinq/i02qNZni8bI7Q3toZPd029KNOfhP5cXhSKhQQnWIftBDNWVEcMTXSzSuRjW81c5VwifE+HxSRqiPjcxXNRyI5MZReSmStgmllvmq3XWaPepLW1"
    "JEVU4LMv2U9OK/A226JrqimFftDNoxceq/PSPr0+rPOiNNRaS0zQ2liJvxR70zk+9IvFy/H9x+2sL83TGmbleHIirSwuexq/efyanxVDt0PAbdGyu2bXJYkV"
    "Ua+Fz8fm9YheVzuW506Q+P40e1ZdEXZ/FVGvxni1fq6moramarqpHSzzvWSR7lyrnKuVUzX0b7ba5YrvcXxxSXSORkKK5MujiVuUx3ZXPHwMI5z2H50+o7tp"
    "q5trbNcKihqOr3VfC7G8meSpyVPMpsa5u3Iql9W23gzkYU2bc7vL6dGbOlHa7VHZrTckihjujqlYUc1ER8sW6qrnvRFRPLPia8UtTPQ1cNXSzOhqIHtkikau"
    "FY5FyinMvmorvqas9tvNyqa+oRN1HzOzup3InJE8jriTdr3qt6FRs/DqxseLNydf5yb06F1G3VmkbVe2oiOq4Gvkan3ZE4OT9pFOLtK0izWujLjZ8J172dZT"
    "uX7szeLfnw9Tz/R7ili2V2vrUVN+SdzM/mrI7BkdyZQsafeo4vn17+hk1d3/AI1Tp8Jfz9kY+GR0cjFY9jla5q82qi4VCGSekBo/8l9eS1kMe7RXdFqo8Jwb"
    "Jykb8cO/WMalXXTuzMS+k4mRTkWabtPVQQIp4SRT7p3btTCqc0kavzQ+FPumbv1cDPzpWJ/pIZjm8XPwy39plzCxfwp+4wT0r0T6L06vb7TN/YQzvA3dja3u"
    "aifIwL0sH/yDTkffPO7/AEW/3lpf/ty+c7G/6236/aWuyG6mxtf+S/Ta/wDgm/vU0rQ3X2Qx9Xsy003/AMDGvxypGxPxS6DtP/Zo9fs9f2lICwcWZKQivaxq"
    "ucqIiJlVXsQD6Bjh/SE2dRyOYt7lVWqqKqUkqpw7l3Sf8IbZyv8A11N/6SX/AGTX3tHilRg5M/8Axz8pZIBj2l2+bPayqhpYr45JJ5Gxs36aVqbyrhMqrcJx"
    "7VMhHqmqKuUtV2zctcLlMx6gAPTUFQgAoyQAXJAABSAC5GQhALkgAAAAAAABxq+40drpnVNdVQ0sDOckz0a1PVTH952+aQtj3R00lVcnpw/k0fuftOwnwPFd"
    "2mj8UpOPh38idLNE1ekMkgwpN0lKfP8AJ9OTKnfJUoi/Jqn1T9JGnV38p07M1vasVSjl+CtQ0+2WddN5Zf7d2jpr3U/OP1ZpBjez7e9H3ORsVRNVW57lxmqj"
    "9z9puU+J7+huNHcqdtRRVMNTC7i2SJ6OavqhuouU1/hlW5GHfx50vUTT6w5ICBT2jAAAAAAAAAAAAAAAAKAgAYIUKoEMTdJCBX6OoJcfzde35schlkx3t6pF"
    "qdnFW9EytPPDL/pon8TTkRrbqWWx69zOtT/3Q1jp53U1RDOz7UUjZE80VF/gbQbXqZL1svr5403t2KKrb5I5qr8lU1bReJtToKoZq/ZXR08zkeslG+hl8HNR"
    "Wf3KVuF70VUeMO27VRNmvHyo/wAav0n7Nedn11t1j1jba+7RtfRxS++rm7yMyioj8eC4U2B1Pto0rpyJiw1bbtO/lDRva7Cd7nck/eavTwvp5pYJUVskT3Rv"
    "RexzVwv7j4aiN5cDTayarUTTELTaOwbG0btORcqnSI00jr1bi1UVq2gaTfFvJPb7nT8HIvFEXii+CovzQ1FulAlquNVQ9cyf2aZ8XWMXLX7q4yhzqHWOoLZa"
    "X2iiu9XT0D1VVgjfhOPPjzRF7kU6mON8z2xxtVz3qjWtTmqryQ9ZF+LumkcWvYexruzarm9XrTPKPvPwfvbrbWXesioqClmqqmVcMiibly/795nHSOx21aRt"
    "smo9YOjqpqWJZ3UvOGHCZ4/nu+Xmen0bpazbKdJPuFydHHUpEktdVuTjn8xvgi8EROamHdom125a16y30ka0Vn3v5r+knwvBXr2J+FPmbYt0WKd6vjV4Ky7n"
    "5e2Ls2MP3bUTpNXj/PD5vI327z6gvNXcpm/WVUqvRjU+yn3WongmE9DaPZbpFNH6QpaORqJWTJ7RUqnPrHJy9EwnoYH2MaVTU+s4HzM3qO3IlVNnkqovuN9V"
    "4+im0vebcG3zuSr+1ubEbmDb5UxrP2j5Om1jqen0hpyuvNQ1HpTR5ZHnHWPXg1vqp+TX2/X+jso5HUd1pMKqcd3eT96L80MQ9IvVK1NfR6ZgfmOnRKqpRF5v"
    "X7DV8kyvqhxdg+0GW13KPSVW18tLWPVaRzUysMmMq1fwrhV8F8zbOTHe93PJXUbCu/6fGdR+KJ1//Pj9/RjHUFlrdN3mqtFfGrKimerV4cHp2OTvRU4nm7mi"
    "pOmfzTc3XOziza6pEbWxrDWRNVIKuNPfZ4L+c3wU04vzOpr3w5ysaqzuzhVTPyIV3Hm1Xw5S6/Z22qdoY873CunTXw9Ydbg7LTen67VN8pLNbYlkqap+6nDg"
    "xva93ciJxU4VPF19RFDnd6x7WZxnGVx/E3Q2f7LrDs6o1ZbYlmrJUTr6ybCyS+H4W+CfM22bU1z5IO1tqRh0aRHvTydhF9G7OtEta527QWej4ryVyNb+9y/N"
    "TsbBe6bUdlorvRrmnrIWzR96Iqcl8U5ehrf0gNqi6guEmlLU9zaCilVKuTGOvmav2f0Wr8V8j1fRi1kypsVbpqsna2Sgk6+m33YzE9eKJnudn9omU3o39yOT"
    "kruy7tOJ7VXzmdfhPX5vZbcNFJrHQ9X1Ee9X2/8AldNw4ruou81PNufXBp5nPI/oHvtenBUcnhxNMNr2j10Vrmvoo4lZRVDvaqVccOreud1P0Vynohqy6P8A"
    "KFt2ZzONWNV6x93jAAQXXh2OmKX23U1opkTPW1sDMeciHWnrdktEtftL05DjKJWtkXyYiu/ge6I1mIR8uvds11eET+TdhOCqa6dK6pR1dpyl7WxzyfFWJ/A2"
    "LNWulBV9dryipkXPs9vavkrnuX+CFhkz7jg9g0a5tPlr+TDrl3WqvchvPs8pvZNCafgxhWW+DP7CGjO4smGJzd7qeam/dqpvY7XR02MJDBHH8Gohpw+cytu1"
    "FXu26fX7OWACe5AMa7e9aJpHQ08EEm7XXXNHBheLWqnvu9G5TzVDJK8jULb9rFNVa7mpqeTforSi0kWF4Ofn6x3x4fqmjIr3aFpsfE9oyYieUcZY1ThyGeIB"
    "Vvowqbyc1ybn7GdZ/lpoWhqZpN6upE9kqu9XtRMO/WbhfVTTAyp0dta/kzrdtqqJFbQ3lqQLnk2ZOMa+vFvqhvxq92rSeqk27h9/jzVTHGnj+rbUDOQWjgAA"
    "AAAAAGAAAAAAAAXAEAAEcuDFe0HbjQ6clltdkbHX3FvuvkVcwwL3Lj7TvBDrtte1CS09ZpmzTKyqe3+V1DF4xNX7jV7HKnNexDAWOOUK7Ky92dyjm7PYHZyL"
    "9MZOVHu9I8fOfJ3N/wBUXfU9StTd6+arf2I9fdb4NbyT0OoVE7iIqofWSpqqmZ1l9CtWqKKYotxpEeCZwXeIqKvERRSzyshhjfLK9d1jGNVXOXuRE5mOb1ru"
    "807TutNaovGk6n2q0V0tK9VRXMRcsk8HNXgp6Wz7EtZ3WnbM6ghomuTKJVSo137KZVPU/K87F9a2eB07rdHWRsRVctJKj1RP0eC/BDdFm9HvREwq7m09m3db"
    "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"
    "W/Az0nIsce53luKpcXtfC9jy67McunpJgAG5WgGQAIUgAAAAABUBC9gEAUABzAA1C2nWpbNtAvlLu4Y6pWdifhkTe/ip5jgqYUzB0j7H7Nf7deWNwyrgWB6/"
    "jYuU+TvkYfOfyKN25VD7NsPIjIwbVflp8Y4NnNj2qYdZaLS21ytmqqJnslSx/FZI1TDXL5t4eaGCNoGkJtE6lqLY9HLTuXraWVf6SJV4eqcl8j8tE6wrdEX2"
    "K6UiLIz7E8GcJNGvNvn2p4mxNytmmNsul4pY5usanvQzx4SWlkxxRU7PFF5kuNMi3u/5Q5q5New8+b2mtm54dJ/bp5NWMZ7FM+dHOwupbLcb5KxWrWSpDEq9"
    "rGc1/aVfgdZR9G+pbcGpWX6B1AjveWKJySuTu48EXx4nsNo2qLds10a20Wrchq5YfZ6OBq8Y24wsi+CcePapixYm1M3LnDRnbe2Le0aKcLCnemuY1YN2m3xu"
    "oddXatiej4Gy9REqclaxN3PxRV9TNOwLSn0Npd93nZipuio9uU4thTg1PVcr8DA2kNOS6s1JQWaHe/lEqdY5OO5GnF7l9M+qm4tLSxUVPFTwMRkUTEYxqcmt"
    "RMIh6wqN+ubstHafJjGxrez7c9I19I5fOeJV1MVFSzVNQ9I4YWLI9y8mtRMqvwNN9W6gl1VqWvvMyu/lMqrG1fuRpwa34YM97f8AVf0PphllgkxU3Rd1+F4t"
    "hb9r4rhPia4Ma572tY1XOcqNaic1VeSGM+7rVFuEjsfg7lqrMr68I9I5/X8mVOj/AKUddtRzX2ePNNbm7seU4LM5P4NyvqhsYnDgea2daWbo/SlFbVaiT7vW"
    "1Cp2yu4u+HL0OTrXU0OkdN1t2lVFdCzETF+/IvBqfFSZYoi1b4uV2vl1bQzpmjjEzpT6dPm5luvVLdKqvp6ZyvWgqEp5Xdm/uo5UTy3kTzNcNlsKpq7WSp/9"
    "oq//AHFMkdH6slrdPXepqHrJNLcVke5fvOVjVVTFOitVWnS+qNWTXWp9nZUW6pp4fdVyySLJwaiInM1VXN6aKp81jjYdVmMrHp4zG7Dg9H+96asusYVv0H8p"
    "nRsVDVScY4JF4cU7FXKIjuz1Nr7perbZKN1Zc66noqdvOSeRGN+fM0Hjc6NWPauHsVHIvcqHPvN+u+paxtTd7hV3CoVcNWV6uVPBqck8kQ12sjcp00WG0Nie"
    "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"
    "Wzz6nuU9gke+1zTLLBvxqxWo7ircL3Kqp5HU8xzPFy9NcaSm4OybWHXNVuZ4+KIdtpW/1GltRW+90qr1tFM2XCffb95vqmU9TqhxTkppidJWNyiK6Zpq5S3+"
    "tdzprxbaW4Ub0kp6qJs0bk7WuTKHLwYO6MWtVudiqtMVUuZ7avW0+9zWBy8U/Vdn0chnDJcW69+mJfMMzHnHvVWp6Os1NYKTVFhrrNXtVaeshdE/HNueSp4o"
    "uF9DSjWmh7zoO8Ptl3gcnFeoqUT6upZ2Oav705ob1Lx5nAvNiteoaF9BdqCnraV/OOZiOTPencvihrvWYr9UzZe1KsKqeGtM84aDA2b1L0XbBXOfNYLlV2t6"
    "8UhlTr4vmqOT4qY6uvRo13QOVaP6NuMaclin6ty+j0T95BqsVx0ddZ25iXI/FpPnw/ZirHefdNPNRypLSzy08icnRPVip6oepuOybXdrz7Rpa5K1OboWJKn+"
    "hk8xU0lRRTOhqaeanlbzjlYrXJ6Ka5pmnnCxt3rN6PcqiXuNN7btcabVrWXiSvgb/Q1/1qKndvL7yfEz1sy262fXczLZXRJa7w5Pchc/Mc6/gd3/AIV4+ZqQ"
    "fcUskUjZIpHxyMcjmPauFa5OSovYqGy3fqplX5uxsfIpnSndq8Yf0EMS9Jv/AKNm/wCcIP8AWPS7IdZya40NQ3KpdvVseaaqVO2RnDe9Uwvqeb6TSf8AJs3/"
    "ADhB/rE65VvW5lxuFaqt5tFurnFWjVEqLxC8yJzKp9IbRdF+1Q0eiq6vRzXT1la7fwvFrWNRGovxcvqZl5mkWmL5crCyOqtVdUUc7XL78LsZ48lTkqeZm3QO"
    "32pqqqmtepaWNzpXtibWwe7xVcIr28vVPgTMfLo0iieDlNsdncneqyrfvRPHzj9dGZJbVSulkqY4YoauRis9pZG3rE9VTj6ngrnY9pdiSWutWpoL3uKrkoqu"
    "naxZE7kVOGfVDIKXCkWsWh9qh9raxJFg30391fvY544cz9XKmCZVRFTmLWRXanlE6+Ma/wA+DzWz/XEGuLS+qSB1LWU0iwVdK7nDIn8F7DtdQaktWmLe6uu1"
    "WymhTg3PFz17mpzVTE1k1I2z37aTqO3JE6hh3eqVfsSTtRe7nlV+Zh+/anu2qK51ddqt9ROuUTPBsafmtTkiES5l93T4y6LB7O+2ZFWk7tuNPXjETp8NXu9a"
    "bdb3eqlYbG+S00TF91zFTrpPFy9nknxPC3TVl/vbFjuV5uFXGv3JJl3V9OR1XPipCrrv11zrMu/xdk4mNTFNuiOHXr8xOHBCk7S8kyalgyNsNti12vqSVW5b"
    "RwyTuXu4bqfNxs0nYYh6O+nXUlkq79NGrX1z0igVU/om819XZ+Bl4vMG3uWo16vkvaXKi/n1acqeHy/dQEUExQACqTIFyRQAAAAAAAXJC9gEAAAAAF5GunSJ"
    "0stu1FSX+CNepuDOqmVOSSsThnzb/ZNijzm0LSjNZaVrbXhvXq3rKdy/dlbxb8eXqaMi13luaVrsXO9jy6Ls8uU+k/zVp/yNs9lerU1bo2jqZH71XTp7NU9+"
    "+1OfqmF9TU6WJ8Mr4pWKyRjla5q82qnBUPabLtoLtB3KsfMj30dVA5HRt4/WtRVjX48F8yrxLvd18eUvofaTZk52LvWo1qp4x5+Mfzwd7t+1Ut31LFZIX5p7"
    "YmX4Xgszk4/BMJ6qdn0dtJ+03Cr1NUM+rpkWmps9r1T33eiYT1UxI91bfrq52HT1tbOq45q+R7v71NvdGabi0lpugs8WF9njRJHJ9+ReLneqqpvx4769NyeU"
    "KfbdynZuzaMG3PvVc/vPxnh6O7TgatbaNWflPrKaGF+9R23NNFheDnIvvu+PD0M87UNWt0fpCrrWORKqVOopk75HdvomV9DUri9yq5yqqrxVe1TOfd0iKIR+"
    "x2z96urMqjlwj16/zzRVROK8D2Wltk2qdVoyWGiWipHf5RV5Yip3o3m74GXdjOzq323TlLerjQQz3Ks+vY+Vm8sMa/ZREXkqpx9TKfBEwYsYOsRVXLftXtdV"
    "buVWcWnlw1n7QxnpXYPpuybk9037vVJx+t92JF8GJz9VUyRBTw0sTYaeJkMbEw1kbUa1E8EQ+KutpaCnfUVc8VPCxMuklcjWp6qYy1T0gNP2lXwWaN93qE4b"
    "7V3IUX9JeK+iepN/pWY8HKRGftS51rn6R9oZUVyIir3Hgtaba9I6L34Z632+uROFJRYkfn8S/Zb6qYJ1TtV1Tqzejq69aald/k1JmNip4rnLvVTHN1REqUwm"
    "PdItWdrOlEOhx+yNVFHeZVXwj9WR9Y9InVeo0kp7VuWKjdwxAu9O5PGReX6qJ5mL5qiapmfPPLJLM9cvkkcrnOXxVeKn5g0VV1Vc5X2NhWceNLVOipzNrdg+"
    "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"
    "ORqsc1e1FTCoeRdsb0Av/ZO1/wBX/wDJrvW6q40hL2VnWsS5NyumZno0qOdYbRUahvdDZ6RqrPWzthZ4ZXivkiZX0NyU2PaARP8Amna/6v8A+TmWjZvpDT9w"
    "juNq09QUdZGitZNHHhzUVMLj0I8YlWvGV7c7TW5pmKKJ1+DuLNaqax2qjtlGxGU9JC2GNE7mpg5uMkRCk9x8zMzrKcQUGNWEBQZHS6v09T6r01cbJUom5WQO"
    "jRy/cdza70dhTRaso6i31tRRVUax1FNI6GVi82uauFT5H9AsHmLlsw0ZeLhNcLhpu3VNXO7elmfH7z15ZUj37PeaTC62RtWMLeprjWJ/NpAiHttjutl0Trqi"
    "qZZN2hrP5JVZXgjHLwcv6LsL5ZNoV2QaBX/sna/6s/JdjOgHLldKWz+rX+8004tdM66rPJ7QY963NuqidJ9HTbYdksm0qCkqaS6Opa2jY5sUcib0EiOwq5RO"
    "KLwTimfI1q1Ps31Xo6R30xZqmOFF4VMTeshcnfvt5euDeCGFlPEyGNu6yNqNancicEQr2I9qtciKi8FRe03XMemvj1VWBtq9i0xREa0+D+fLVR3FFRSm7t22"
    "XaLvr1fcNN26SRecjI+rf8W4U8tW9G3QVUqrFT3Cl8Iqp2E/ayRpxKui+t9prE/jpmPlLUsG1DejBotFy6qvDk7vaG/7J2lt6PGz+3vR77XPWKnZU1L3J8EV"
    "EMRiVtlXaXFjlEz8P3amW621t3qm0lupKisqHrhsUDFe5fRDYPY9sBmtVZBqHV0cftMSpJTW9F3kjd2PkXkqp2InIzVZtPWnT0HUWm20dBF2tp4kZnzxzOxJ"
    "FvGinjPFS5+37t+mbdqN2J+YhiTpO5/wbx4/+4Q/ucZbOr1Hpi0att6W+90TK2kSRJeqeqom8mcLwVF7VN9dO9TMQqMS9Fm9Rdq5ROrQ1C9puV/gL2df92af"
    "+tk/2j4dsJ2eLy05An/70n+0QfZKvF1v+5sf/jP0/V+OwBM7KrR+lN/7rj1mpdKWjVdAtFd6RlRHx3Hcnxr3tdzRT99P6ft2mLXDarTTNpaKHeWOJqqqNyqq"
    "vFVVeaqdiqZJ1NEbsUy5K7kTN+q9bnTWZmPFrfq3YBfLXI+ewSpdabmkLlRk7U7u53yMb19puFomWG40VTRyJzbPGrF+ZuwiH41NJT1sax1NPFOxfuyMRyfB"
    "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"
    "AAAAAAAAAAAAAAAAAAAAAAAAA//Z"
)

FUNPAY_ORDER_URL = "https://funpay.com/orders/{}/"
FUNPAY_CHAT_URL = "https://funpay.com/chat/?node={}"

_CMD_CODE = frozenset(("!steamguard", "!code", "/code", "!код", "/код", "код", "code"))
_CMD_TIME = frozenset(("!time", "/time", "!время", "/время", "время", "time"))
_CMD_EXTEND = frozenset(("!extend", "/extend", "!продлить", "/продлить", "продлить", "extend"))
_CMD_STOCK = frozenset(("!stock", "/stock", "!наличие", "/наличие", "наличие", "stock"))
_CMD_ACCOUNT = frozenset(("!аккаунт", "/аккаунт", "!account", "/account", "аккаунт"))
_CMD_SELLER = frozenset(("!продавец", "/продавец", "продавец", "!seller", "/seller", "seller"))
_CMD_HELP = frozenset(("!команды", "/команды", "команды", "!commands", "/commands", "commands", "!помощь", "/помощь", "помощь", "!help", "/help", "help"))

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

def _classify_error(e: Exception) -> Tuple[str, str, bool]:
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
        return "LOGIN_FAILED", "Не удалось авторизоваться в Steam (временный сбой входа/rate-limit — обычно проходит при повторе)", True
    if "rsa key" in low:
        return "RSA_MISSING", "Steam не выдал RSA-ключ для входа — аккаунт может быть временно заблокирован или требует доп. проверки", True
    if "wizard params" in low:
        return "NO_WIZARD", "Steam не выдал параметры восстановления пароля (временный сбой либо требуется email-подтверждение)", True
    if "sessionid" in low:
        return "NO_SESSION", "Не удалось получить сессию Steam (сервис недоступен либо забанен IP-адрес бота)", True
    if "poll confirmation timed out" in low or "poll recovery" in low:
        return "POLL_TIMEOUT", "Не дождались подтверждения в мобильном приложении Steam Guard (истекло время ожидания)", True
    if "verifycode" in low or ("verify" in low and "code" in low):
        return "VERIFY_CODE_FAILED", "Steam отклонил код подтверждения при смене пароля", True
    if "changepassword" in low:
        return "CHANGE_REJECTED", "Steam отклонил запрос на смену пароля", True
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
    return "UNKNOWN", f"Неизвестная техническая ошибка ({name}): {_safe_err(e)}", True

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

_MATCH_TAG_PREFIX = "id"
_MATCH_TAG_RANDOM_LEN = 6

def _gen_match_tag(existing: set) -> str:
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
    FROZEN = "FROZEN"

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
    UA = ("Mozilla/5.0 (Linux; Android 9; Valve Steam App Version) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
          "Chrome/74.0.3729.185 Mobile Safari/537.36")
    HELP = "https://help.steampowered.com"

    @classmethod
    def _ua(cls) -> str:
        try:
            if SETTINGS and getattr(SETTINGS, "steam_ua", None):
                return SETTINGS.steam_ua
        except Exception:
            pass
        return cls.UA

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
                        await self._steam.raw_request("GET", wu, headers={"User-Agent": self._ua()})
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
                        "User-Agent": self._ua(),
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
                user_agent=self._ua(),
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
                            "User-Agent": self._ua(),
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
                            "User-Agent": self._ua(),
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
                    "User-Agent": self._ua(),
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
                    "User-Agent": self._ua(),
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
    extend_options: List[Dict[str, Any]] = Field(default_factory=list)
    note: Optional[str] = None
    subcategory_id: Optional[int] = None
    hours_per_unit: float = 1.0
    match_tag: Optional[str] = None
    extend_write_tag: Optional[str] = None
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
        "▶️  Напишите  !команды  чтобы увидеть список всех команд\n"
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
    frozen_remaining_seconds: Optional[float] = None
    frozen_from: Optional[str] = None
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
    buyer_confirmed: bool = False
    review_bonus_hours: Optional[float] = None

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
            "is_extension", "lot_id", "buyer_confirmed", "review_bonus_hours")}

class Settings(BaseModel):
    enabled: bool = False
    autoback_on_error: bool = False
    auto_extend: bool = False
    auto_disable_lots: bool = False
    auto_enable_lots: bool = False
    auto_free_on_error: bool = False
    save_deleted_acc: bool = True
    steam_ua: str = ("Mozilla/5.0 (Linux; Android 9; Valve Steam App Version) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                     "Chrome/74.0.3729.185 Mobile Safari/537.36")
    lots: Dict[str, Any] = {}
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
    auto_refund_1star: bool = False
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
            hpu = raw.get("hours_per_unit")
            if hpu is None:
                if raw.get("lot_type") == "fixed" and raw.get("fixed_hours"):
                    hpu = float(raw.get("fixed_hours"))
                else:
                    hpu = 1.0
            return LotConfig(tag=_ntag(raw.get("tag", "default")),
                             extend_lot_id=raw.get("extend_lot_id"),
                             extend_options=raw.get("extend_options") or [],
                             note=raw.get("note"),
                             subcategory_id=raw.get("subcategory_id"),
                             hours_per_unit=float(hpu) if hpu else 1.0,
                             match_tag=raw.get("match_tag"),
                             extend_write_tag=raw.get("extend_write_tag"))
        return None

    def set_lot(self, lot_id: str, tag: str, extend_lot_id: Optional[str] = None,
                note: Optional[str] = None, subcategory_id: Optional[int] = None,
                hours_per_unit: Optional[float] = None):
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
        if hours_per_unit is not None:
            existing["hours_per_unit"] = float(hours_per_unit)
        elif "hours_per_unit" not in existing:
            existing["hours_per_unit"] = 1.0
        self.lots[str(lot_id)] = existing
        _save_settings()

    def add_lot_extend_option(self, lot_id: str, ext_lot_id: str, hours: float):
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

    def tags_with_lots(self) -> List[str]:
        seen = []
        for lid in self.lots:
            lc = self.get_lot(lid)
            if lc and lc.tag and lc.tag not in seen:
                seen.append(lc.tag)
        return seen

    def set_lot_hours_per_unit(self, lot_id: str, hours_per_unit: float):
        existing = self.lots.get(str(lot_id), {})
        if isinstance(existing, str):
            existing = {"tag": _ntag(existing)}
        existing["hours_per_unit"] = float(hours_per_unit)
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
        return None

    def ensure_match_tag(self, lot_id: str) -> Optional[str]:
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

    def all_used_service_tags(self) -> set:
        used = set()
        for lid in list(self.lots.keys()):
            raw = self.lots.get(lid)
            if isinstance(raw, dict):
                if raw.get("match_tag"):
                    used.add(_ntag(raw["match_tag"]))
                if raw.get("extend_write_tag"):
                    used.add(_ntag(raw["extend_write_tag"]))
                for opt in (raw.get("extend_options") or []):
                    if opt.get("write_tag"):
                        used.add(_ntag(opt["write_tag"]))
        return used

    def gen_new_service_tag(self) -> str:
        return _gen_match_tag(self.all_used_service_tags())

    def set_lot_extend_write_tag(self, lot_id: str, write_tag: Optional[str]):
        raw = self.lots.get(str(lot_id), {})
        if isinstance(raw, str):
            raw = {"tag": _ntag(raw)}
        raw["extend_write_tag"] = write_tag
        self.lots[str(lot_id)] = raw
        _save_settings()

    def set_lot_extend_option_write_tag(self, lot_id: str, ext_lot_id: str, write_tag: str):
        raw = self.lots.get(str(lot_id), {})
        if isinstance(raw, str):
            raw = {"tag": _ntag(raw)}
        opts = list(raw.get("extend_options") or [])
        ext_lot_id = str(ext_lot_id)
        for o in opts:
            if str(o.get("lot_id")) == ext_lot_id:
                o["write_tag"] = write_tag
        raw["extend_options"] = opts
        self.lots[str(lot_id)] = raw
        _save_settings()

    def build_service_tag_map(self) -> Dict[str, str]:
        m: Dict[str, str] = {}
        for lid in self.lots:
            cfg = self.get_lot(lid)
            if not cfg:
                continue
            if cfg.match_tag:
                m[_ntag(cfg.match_tag)] = lid
            if cfg.extend_write_tag:
                m[_ntag(cfg.extend_write_tag)] = lid
            for opt in (cfg.extend_options or []):
                if opt.get("write_tag"):
                    m[_ntag(opt["write_tag"])] = lid
        return m

    def build_extend_service_tags(self) -> set:
        s = set()
        for lid in self.lots:
            cfg = self.get_lot(lid)
            if not cfg:
                continue
            if cfg.extend_write_tag:
                s.add(_ntag(cfg.extend_write_tag))
            for opt in (cfg.extend_options or []):
                if opt.get("write_tag"):
                    s.add(_ntag(opt["write_tag"]))
        return s

    def ensure_all_match_tags(self) -> None:
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
PWD_BACKUP_HUMAN_LIMIT = 20
_pwd_backup_lock = threading.Lock()
cardinal_ref: Optional[Cardinal] = None
tg_logs: Optional[Any] = None

_code_cooldowns: Dict[str, float] = {}
_cooldowns_lock = threading.Lock()
_processed_orders: Dict[str, float] = {}
_temp_storage: Dict[int, dict] = {}
_tag_queue_index: Dict[str, int] = {}
_tag_queue_lock = threading.Lock()
_data_lock = threading.RLock()
_processed_lock = threading.Lock()
_toggling_tags: Set[str] = set()
_toggling_lock = threading.Lock()
_stop_event = threading.Event()

_ignored_orders: Dict[str, float] = {}
_ignored_lock = threading.Lock()
IGNORED_ORDER_TTL = 1800

@dataclass
class PendingOrder:
    order_id: str
    buyer: str
    buyer_id: int
    chat_id: Any
    tag: str
    lot_id: Optional[str]
    hours: int
    received_at: float
    confirmed: bool = False
    confirmed_at: Optional[float] = None
    ttl: float = 7200.0

    def age_str(self) -> str:
        sec = int(time.time() - self.received_at)
        h, m = divmod(sec, 3600)
        return f"{h}ч {m // 60}м" if h else f"{m // 60}м {sec % 60}с"

    def is_expired(self) -> bool:
        return time.time() - self.received_at > self.ttl

class PendingOrderStore:
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

    def remove(self, order_id: str):
        with self._lock:
            self._store.pop(order_id, None)

    def cleanup_expired(self) -> int:
        with self._lock:
            expired = [k for k, p in self._store.items() if p.is_expired()]
            for k in expired:
                del self._store[k]
        return len(expired)

    def all_pending(self) -> List[PendingOrder]:
        with self._lock:
            return [p for p in self._store.values() if not p.confirmed]

    def snapshot(self) -> List[PendingOrder]:
        with self._lock:
            return list(self._store.values())

_pending_store = PendingOrderStore()

def _save_settings():
    _save_json("settings", SETTINGS.dict())

def _save_accounts():
    _save_json("accounts", [a.dict() for a in ACCOUNTS])

def _save_pwd_backups():
    _save_json("pwd_backups", PWD_BACKUPS)

def _record_password_backup(acc_id: int, login: str, password: str, source: str):
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
                raw_idx = _tag_queue_index.get(tag_n, 0)
                idx = raw_idx % len(candidates)
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
    def extend_rent(acc_id: int, hours: float) -> Optional[str]:
        with _data_lock:
            acc = AccountRepo.get(acc_id)
            if acc and acc.rental_end:
                acc.rental_end = _fmt(_parse(acc.rental_end) + timedelta(hours=hours))
                _save_accounts()
                return acc.rental_end
        return None

    @staticmethod
    def reactivate_for_bonus(order, hours: float) -> Optional[str]:
        with _data_lock:
            acc = AccountRepo.get(order.acc_id)
            if not acc or acc.status != RentStatus.FREE:
                return None
            acc.status = RentStatus.ACTIVE
            acc.current_order = order.id
            acc.owner = order.buyer
            acc.owner_id = order.buyer_id
            acc.owner_chat_id = order.chat_id
            acc.rental_start = _fmt(_now())
            acc.rental_end = _fmt(_now() + timedelta(hours=hours))
            acc.access_count = 0
            _save_accounts()
            return acc.rental_end

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
    def freeze_account(acc_id: int) -> bool:
        with _data_lock:
            acc = AccountRepo.get(acc_id)
            if not acc:
                return False
            if acc.status == RentStatus.ACTIVE and acc.rental_end:
                remaining = (_parse(acc.rental_end) - _now()).total_seconds()
                acc.frozen_remaining_seconds = max(remaining, 0.0)
                acc.frozen_from = RentStatus.ACTIVE
                acc.status = RentStatus.FROZEN
                _save_accounts()
                return True
            if acc.status == RentStatus.FREE:
                acc.frozen_remaining_seconds = None
                acc.frozen_from = RentStatus.FREE
                acc.status = RentStatus.FROZEN
                _save_accounts()
                return True
            return False

    @staticmethod
    def unfreeze_account(acc_id: int) -> bool:
        with _data_lock:
            acc = AccountRepo.get(acc_id)
            if not acc or acc.status != RentStatus.FROZEN:
                return False
            if acc.frozen_from == RentStatus.FREE:
                acc.status = RentStatus.FREE
                acc.frozen_remaining_seconds = None
                acc.frozen_from = None
                _save_accounts()
                return True
            # По умолчанию (в т.ч. для старых данных без frozen_from) — считаем,
            # что заморозили активную аренду, восстанавливаем её как раньше.
            remaining = acc.frozen_remaining_seconds or 0.0
            acc.rental_end = _fmt(_now() + timedelta(seconds=remaining))
            acc.frozen_remaining_seconds = None
            acc.frozen_from = None
            acc.status = RentStatus.ACTIVE
            _save_accounts()
            return True

    @staticmethod
    def replace_account(old_acc_id: int) -> Tuple[Optional[AccountModel], Optional[AccountModel]]:
        with _data_lock:
            old_acc = AccountRepo.get(old_acc_id)
            if not old_acc or old_acc.status not in (RentStatus.ACTIVE, RentStatus.FROZEN):
                return old_acc, None
            tag_n = _ntag(old_acc.tag)
            candidate = next(
                (a for a in ACCOUNTS if _ntag(a.tag) == tag_n
                 and a.status == RentStatus.FREE and a.id != old_acc.id),
                None
            )
            if not candidate:
                return old_acc, None
            candidate.status = old_acc.status
            candidate.current_order = old_acc.current_order
            candidate.owner = old_acc.owner
            candidate.owner_id = old_acc.owner_id
            candidate.owner_chat_id = old_acc.owner_chat_id
            candidate.rental_start = old_acc.rental_start
            candidate.rental_end = old_acc.rental_end
            candidate.access_count = old_acc.access_count
            candidate.frozen_remaining_seconds = old_acc.frozen_remaining_seconds
            order_id = old_acc.current_order
            old_acc.status = RentStatus.ERROR
            old_acc.current_order = old_acc.owner = old_acc.owner_id = None
            old_acc.owner_chat_id = old_acc.rental_start = old_acc.rental_end = None
            old_acc.access_count = 0
            old_acc.frozen_remaining_seconds = None
            if order_id and order_id in ORDERS:
                ORDERS[order_id].update(acc_id=candidate.id, acc_login=candidate.login,
                                         acc_tag=_ntag(candidate.tag))
            _save_accounts()
            return old_acc, candidate

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
        if not buyer_name:
            return None
        bl = buyer_name.strip().lower()
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
        logger.debug(
            f"[ASRplus] find_order_by_chat: chat_id={chat_id}, "
            f"author_id={author_id}, author_name={author_name}, "
            f"active_orders={[o.id for o in ORDERS.values() if o.status == RentStatus.ACTIVE]}"
        )
        key = str(chat_id)

        if author_name:
            found = AccountRepo.find_active_by_name(author_name)
            if found:
                return found

        for o in ORDERS.values():
            if o.status in (RentStatus.FINISHED, RentStatus.REFUND):
                continue
            if str(o.chat_id or "") == key:
                return o

        if author_id and author_id > 0:
            for o in ORDERS.values():
                if o.status in (RentStatus.FINISHED, RentStatus.REFUND):
                    continue
                if o.buyer_id == author_id:
                    return o

        if author_name:
            al = author_name.strip().lower()
            for o in ORDERS.values():
                if o.status in (RentStatus.FINISHED, RentStatus.REFUND):
                    continue
                if o.buyer and o.buyer.strip().lower() == al:
                    return o

        if author_id and author_id > 0:
            for acc in ACCOUNTS:
                if acc.status == RentStatus.ACTIVE and acc.owner_id == author_id:
                    if acc.current_order and acc.current_order in ORDERS:
                        return ORDERS[acc.current_order]

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
    if "hours" in kw:
        try:
            kw.setdefault("hours_word", _hours_word(float(kw["hours"])))
        except Exception:
            kw.setdefault("hours_word", "часов")
    for k, v in sorted(kw.items(), key=lambda x: len(x[0]), reverse=True):
        r = r.replace(f"${k}", str(v))
    return r

def _send_fp(c, chat_id, text):
    try:
        c.send_message(chat_id, text)
    except Exception as e:
        logger.warning(f"[ASRplus] send_message: {e}")

def _do_refund(c, order_id, bypass_autoback_gate: bool = False) -> bool:
    if not bypass_autoback_gate and not (SETTINGS and SETTINGS.autoback_on_error):
        logger.debug(f"[ASRplus] _do_refund #{order_id}: авто-возврат выключен, пропуск")
        return False
    try:
        c.account.refund(order_id)
        return True
    except Exception as e:
        logger.warning(f"[ASRplus] _do_refund #{order_id}: FunPay отклонил возврат — {_safe_err(e)}")
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

_TAG_RE = re.compile(r'#([a-zA-Zа-яА-ЯёЁ0-9_\-]{2,32})')

def _parse_hash_tag_from_text(text: str, exclude_id: Optional[str] = None) -> Optional[str]:
    if not text:
        return None
    excl = _ntag(str(exclude_id)) if exclude_id else None
    matches = _TAG_RE.findall(text)
    for match in matches:
        candidate = _ntag(match)
        if excl and candidate == excl:
            continue
        if candidate.isdigit():
            continue
        if any(_ntag(a.tag) == candidate for a in ACCOUNTS):
            return candidate
        for lid in SETTINGS.lots:
            cfg = SETTINGS.get_lot(lid)
            if cfg and _ntag(cfg.tag) == candidate:
                return candidate
    return None

def _get_lot_detailed_description(c, lot_id: str):
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
    return lf, "ru", ""

def _extract_tag_from_lot_description(c, lot_id: str) -> Optional[str]:
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

_MATCH_TAG_TEXT_RE = re.compile(r'#' + _MATCH_TAG_PREFIX + r'[a-z0-9]{' + str(_MATCH_TAG_RANDOM_LEN) + r'}\b', re.IGNORECASE)

def _strip_match_tags_from_funpay_lot(c, funpay_lot_id: str) -> Tuple[bool, str]:
    try:
        lf, lang, desc_text = _get_lot_detailed_description(c, funpay_lot_id)
    except Exception as e:
        return False, f"Не удалось получить описание лота: {_safe_err(e)}"
    if lf is None:
        return False, "Не удалось получить подробное описание лота"
    if not desc_text or not _MATCH_TAG_TEXT_RE.search(desc_text):
        return True, "Служебных #id в описании не найдено"
    new_desc = _MATCH_TAG_TEXT_RE.sub("", desc_text)
    new_desc = re.sub(r'[ \t]+\n', '\n', new_desc).rstrip()
    try:
        if lang == "en":
            lf.description_en = new_desc
        else:
            lf.description_ru = new_desc
        c.account.save_lot(lf)
        _invalidate_lots_cache()
        return True, "Старые служебные #id удалены из описания"
    except Exception as e:
        return False, f"Ошибка записи в описание: {_safe_err(e)}"

def _unlink_extend_lot(c, ext_lot_id: Optional[str]):
    if not ext_lot_id or c is None:
        return
    try:
        _cancel_extend_lot_timer(ext_lot_id)
    except Exception:
        pass
    try:
        _toggle_single_lot(c, ext_lot_id, False)
    except Exception as e:
        logger.warning(f"[ASRplus] Не удалось выключить старый лот-продление #{ext_lot_id}: {e}")
    try:
        _strip_match_tags_from_funpay_lot(c, ext_lot_id)
    except Exception as e:
        logger.warning(f"[ASRplus] Не удалось очистить #id старого лота-продления #{ext_lot_id}: {e}")

def _write_tag_to_funpay_lot(c, funpay_lot_id: str, tag: str) -> Tuple[bool, str]:
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
    lc = SETTINGS.get_lot(lot_id)
    if not lc:
        return False, None, "Лот не найден"
    target_tag = SETTINGS.ensure_match_tag(lot_id)
    if not target_tag:
        return False, None, "Не удалось создать ID для лота"
    ok, msg = _write_tag_to_funpay_lot(c, lot_id, target_tag)
    return ok, target_tag, msg

def _match_lot_by_match_tag(description: str, order_id: Optional[str] = None) -> Optional[str]:
    lot_id, _is_ext = _match_lot_by_match_tag_ex(description, order_id)
    return lot_id

def _match_lot_by_match_tag_ex(description: str, order_id: Optional[str] = None) -> Tuple[Optional[str], bool]:
    if not description or not SETTINGS.lots:
        return None, False

    SETTINGS.ensure_all_match_tags()

    match_tag_map: Dict[str, str] = SETTINGS.build_service_tag_map()

    if not match_tag_map:
        return None, False

    extend_tags: set = SETTINGS.build_extend_service_tags()

    excl = _ntag(str(order_id)) if order_id else None
    for raw in _TAG_RE.findall(description):
        candidate = _ntag(raw)
        if excl and candidate == excl:
            continue
        lot_id = match_tag_map.get(candidate)
        if lot_id:
            is_extend = candidate in extend_tags
            logger.info(
                f"[ASRplus] _match_lot_by_match_tag: найден ID '{candidate}' -> лот {lot_id}"
                f"{' (лот-продление)' if is_extend else ''}"
            )
            return lot_id, is_extend
    return None, False

def _match_lot_by_tag_keyword(description: str, order_id: Optional[str] = None) -> Optional[str]:
    if not description or not SETTINGS.lots:
        return None

    match_tag_lot = _match_lot_by_match_tag(description, order_id)
    if match_tag_lot:
        return match_tag_lot

    desc_lower = description.strip().lower()

    hash_tag = _parse_hash_tag_from_text(description, exclude_id=order_id)
    if hash_tag:
        lot_id = SETTINGS.find_lot_id_by_tag(hash_tag)
        if lot_id:
            logger.info(
                f"[ASRplus] _match_lot_by_tag_keyword: найден #тег '{hash_tag}' "
                f"прямо в описании заказа -> лот {lot_id}"
            )
            return lot_id

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
    lot_id, _is_ext = _find_lot_id_for_order_ex(c, event)
    return lot_id

def _find_lot_id_for_order_ex(c, event) -> Tuple[Optional[str], bool]:
    order = event.order
    html  = getattr(order, "html", None) or ""
    order_id = getattr(order, "id", None)

    def _found(lot_id: str, is_extend: bool = False) -> Tuple[str, bool]:
        return lot_id, is_extend

    full = None
    if order_id:
        try:
            full = c.account.get_order(order_id)
        except Exception as e:
            logger.debug(f"[ASRplus] get_order({order_id}) fallback: {e}")

    short_desc = getattr(order, "description", None) or getattr(full, "short_description", None) or ""
    full_desc  = getattr(full, "full_description", None) or ""
    combined_desc = f"{short_desc}\n{full_desc}".strip()

    for attr in ("offer_id", "lot_id"):
        v = getattr(order, attr, None)
        if v is not None:
            m_struct = SETTINGS.find_main_lot_by_configured_extend_id(str(v))
            if m_struct:
                logger.info(
                    f"[ASRplus] #{order_id}: лот найден шагом -1 (структурная "
                    f"привязка продления, offer={v}): {m_struct}"
                )
                return _found(m_struct, is_extend=True)

    if combined_desc:
        m0, m0_is_ext = _match_lot_by_match_tag_ex(combined_desc, order_id)
        if m0:
            logger.info(f"[ASRplus] #{order_id}: лот найден шагом 0 (ID): {m0}")
            return _found(m0, is_extend=m0_is_ext)

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
    return None, False

def _match_lot_by_description(c, description: str) -> Optional[str]:
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

PASSWORD_CHANGE_MAX_RETRIES = 4
PASSWORD_CHANGE_RETRY_DELAY = 10

def _change_password_with_retry(acc) -> str:
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
            delay = PASSWORD_CHANGE_RETRY_DELAY * (attempt + 1)
            logger.warning(
                f"[ASRplus] Смена пароля {acc.login}: временная ошибка [{code}] {desc} — "
                f"повтор {attempt + 1}/{PASSWORD_CHANGE_MAX_RETRIES} через {delay}с"
            )
            time.sleep(delay)
            attempt += 1
    raise last_exc

def _get_buyer_active_targets(buyer_id: int) -> List[Tuple[Any, Any]]:
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
    if not lot_cfg:
        return None
    return lot_cfg.extend_lot_id

def _effective_extend_options(lot_cfg: "LotConfig") -> List[Dict[str, Any]]:
    if not lot_cfg:
        return []
    return lot_cfg.extend_options or []

def _enable_extend_lot_target(c, order, extend_lot_id: str, hard_timer: bool = True) -> Optional[str]:
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
    lot_cfg = SETTINGS.get_lot(order.lot_id) if order.lot_id else None
    extend_lot_id = _effective_extend_lot_id(lot_cfg)
    return _enable_extend_lot_target(c, order, extend_lot_id, hard_timer=hard_timer)

_extend_choice_pending: Dict[int, Dict[str, Any]] = {}
_extend_choice_lock = threading.Lock()
EXTEND_CHOICE_TIMEOUT = 300

_extend_account_choice_pending: Dict[int, Dict[str, Any]] = {}
_extend_account_choice_lock = threading.Lock()
EXTEND_ACCOUNT_CHOICE_TIMEOUT = 300

def _fmt_hours(h) -> str:
    try:
        h = float(h)
        return (f"{h:.0f}" if h == int(h) else f"{h:g}")
    except Exception:
        return str(h)

def _notify_rent_ending_soon(c, order):
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

    def _handle_pwd_change_failure(code: str, desc: str):
        AccountRepo.release(acc.id, error=True)
        if not order:
            return
        if reason == "TIME":
            order.update(status=RentStatus.FINISHED)
            if tg_logs:
                tg_logs.error(
                    f"⚠️ <b>{acc.login}</b>\n"
                    f"∟ Аренда #{order.id} завершена штатно (время истекло), но "
                    f"автосмена пароля не удалась — [{code}] {desc}\n"
                    f"∟ Требуется сменить пароль вручную. Возврат НЕ выполнялся, "
                    f"т.к. покупатель уже полностью использовал оплаченное время."
                )
        else:
            if _do_refund(c, order.id):
                order.update(status=RentStatus.REFUND)
                if tg_logs:
                    tg_logs.refund(order.id, f"[{code}] {desc}: {acc.login}")

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
        if tg_logs:
            tg_logs.error(f"⚠️ <b>{acc.login}</b>\n∟ Причина: {desc}")
        _handle_pwd_change_failure(code, desc)
        return
    except Exception as e:
        code, desc, _ = _classify_error(e)
        logger.error(f"[ASRplus] Смена пароля не удалась: {acc.login} — [{code}] {desc} (raw: {_safe_err(e)})")
        if tg_logs:
            tg_logs.error(f"🔑 Не удалось сменить пароль: <b>{acc.login}</b>\n∟ Причина: {desc}\n∟ Код: <code>{code}</code>")
        _handle_pwd_change_failure(code, desc)

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

    with _ignored_lock:
        if order_id in _ignored_orders:
            logger.debug(f"[ASRplus] Заказ #{order_id} в списке игнорируемых (нет тега), пропуск")
            return

    with _processed_lock:
        if order_id in _processed_orders:
            logger.debug(f"[ASRplus] Заказ #{order_id} уже обрабатывается, пропуск")
            return
        _processed_orders[order_id] = time.time()

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
            lot_id, lot_id_is_extend = _find_lot_id_for_order_ex(c, event)
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
                with _ignored_lock:
                    _ignored_orders[order_id] = time.time()
                with _processed_lock:
                    _processed_orders.pop(order_id, None)
                return

            lot_cfg = SETTINGS.get_lot(lot_id)
            if not lot_cfg:
                logger.warning(f"[ASRplus] #{order_id}: lot_cfg не найден для lot_id={lot_id}")
                with _ignored_lock:
                    _ignored_orders[order_id] = time.time()
                with _processed_lock:
                    _processed_orders.pop(order_id, None)
                return

            tag = _ntag(lot_cfg.tag)
            if not tag or tag == "default" or not any(_ntag(a.tag) == tag for a in ACCOUNTS):
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

            try:
                raw_offer_id = _extract_raw_offer_id(c, event)
            except Exception:
                raw_offer_id = None

            qty = quantity if quantity and quantity > 0 else 1
            rate = lot_cfg.hours_per_unit if lot_cfg.hours_per_unit else 1.0
            eff_opts = _effective_extend_options(lot_cfg)
            if eff_opts and raw_offer_id:
                opt = next((o for o in eff_opts
                            if str(o.get("lot_id")) == raw_offer_id), None)
                if opt and opt.get("hours"):
                    rate = float(opt["hours"])
                    logger.info(
                        f"[ASRplus] #{order_id}: заказ пришёл с лота-продления "
                        f"#{raw_offer_id}, курс = {rate}ч за 1шт (вариант выбора)"
                    )
            hours = qty * rate
            logger.info(f"[ASRplus] #{order_id}: лот {lot_id}, {qty}шт × {rate}ч = {hours}ч")

            with _data_lock:
                existing = AccountRepo.find_active_by_buyer(buyer_id, tag)
                is_extend_purchase = False
                if existing:
                    if lot_id_is_extend:
                        existing_acc = AccountRepo.get(existing.acc_id) if existing.acc_id else None
                        if existing_acc and _ntag(existing_acc.tag) == _ntag(tag):
                            is_extend_purchase = True
                    if not is_extend_purchase and raw_offer_id:
                        if _ntag(raw_offer_id) != _ntag(lot_id) and existing.acc_id:
                            existing_acc = AccountRepo.get(existing.acc_id)
                            if existing_acc and _ntag(existing_acc.tag) == _ntag(tag):
                                is_extend_purchase = True
                    if not is_extend_purchase and raw_offer_id and existing.lot_id:
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
                        _bought_extend_id = raw_offer_id
                        _candidate_ext_ids = set()
                        if _bought_extend_id:
                            _candidate_ext_ids.add(str(_bought_extend_id))
                        _cfg_for_cancel = SETTINGS.get_lot(lot_id) if lot_id else None
                        if _cfg_for_cancel:
                            if _cfg_for_cancel.extend_lot_id:
                                _candidate_ext_ids.add(str(_cfg_for_cancel.extend_lot_id))
                            for _o in (_cfg_for_cancel.extend_options or []):
                                if _o.get("lot_id"):
                                    _candidate_ext_ids.add(str(_o.get("lot_id")))
                        for _eid in _candidate_ext_ids:
                            _cancel_extend_lot_timer(_eid)
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

def _assign_account(c, order_id: str, tag: str, lot_id: Optional[str],
                    buyer: str, buyer_id: int, chat_id, hours: int):
    try:
        return _assign_account_impl(c, order_id, tag, lot_id, buyer, buyer_id, chat_id, hours)
    except Exception as e:
        logger.error(f"[ASRplus] _assign_account #{order_id}: необработанная ошибка: {_safe_err(e)}")
        try:
            with _data_lock:
                stuck_acc = next((a for a in ACCOUNTS if a.current_order == order_id), None)
            if stuck_acc:
                AccountRepo.release(stuck_acc.id)
                logger.warning(f"[ASRplus] _assign_account #{order_id}: аккаунт "
                                f"{stuck_acc.login} освобождён после ошибки")
        except Exception as e2:
            logger.error(f"[ASRplus] _assign_account #{order_id}: не удалось освободить "
                         f"аккаунт после ошибки: {_safe_err(e2)}")
        raise

def _assign_account_impl(c, order_id: str, tag: str, lot_id: Optional[str],
                         buyer: str, buyer_id: int, chat_id, hours: int):
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

    with _data_lock:
        for _cand in [a for a in ACCOUNTS if _ntag(a.tag) == _ntag(tag) and a.status == RentStatus.FREE]:
            if _cand.time_limit_hours is not None and hours > _cand.time_limit_hours:
                logger.info(f"[ASRplus] #{order_id}: аккаунт {_cand.login} пропущен — лимит {_cand.time_limit_hours}ч < {hours}ч")

    acc = AccountRepo.claim_free(tag, order_id, buyer, buyer_id, chat_id, hours)
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

    _pending_store.add(
        order_id=order_id, buyer=buyer, buyer_id=buyer_id,
        chat_id=chat_id, tag=_ntag(acc.tag), lot_id=lot_id,
        hours=hours,
        ttl=max(float(hours) * 3600 + 3600, 7200.0)
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
        is_feedback_related = (
            msg.type == MessageTypes.NEW_FEEDBACK
            or msg.type == getattr(MessageTypes, "FEEDBACK_CHANGED", object())
            or msg.type == getattr(MessageTypes, "FEEDBACK_DELETED", object())
            or "отзыв" in (msg.text or "").lower()
        )
        if is_feedback_related:
            _handle_feedback_event(c, msg)
        return

    with _extend_account_choice_lock:
        acc_pending = _extend_account_choice_pending.get(msg.chat_id)
        if acc_pending and acc_pending["expire"] < time.time():
            _extend_account_choice_pending.pop(msg.chat_id, None)
            acc_pending = None
    if acc_pending:
        raw_txt = msg.text.strip()
        idx = None
        if raw_txt.isdigit():
            idx = int(raw_txt) - 1
        order_ids = acc_pending["order_ids"]
        if idx is not None and 0 <= idx < len(order_ids):
            with _extend_account_choice_lock:
                _extend_account_choice_pending.pop(msg.chat_id, None)
            sel_order = ORDERS.get(order_ids[idx])
            sel_acc = AccountRepo.get(sel_order.acc_id) if sel_order else None
            if not sel_order or not sel_acc:
                _send_fp(c, msg.chat_id, SETTINGS.messages.error_msg)
                return
            _process_extend_for_target(c, msg, sel_order, sel_acc)
            return
        elif raw_txt.isdigit():
            _send_fp(c, msg.chat_id,
                      f"❌ Такого номера нет. Введите число от 1 до {len(order_ids)}.")
            return

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

    fl = msg.text.strip().split('\n', 1)[0].strip().lower()
    is_code = fl in _CMD_CODE
    is_time = fl in _CMD_TIME
    is_extend = fl in _CMD_EXTEND
    is_stock = fl in _CMD_STOCK
    is_account = fl in _CMD_ACCOUNT
    is_seller = fl in _CMD_SELLER
    is_help = fl in _CMD_HELP
    if not (is_code or is_time or is_extend or is_stock or is_account or is_seller or is_help):
        return
    try:
        _process_buyer_command(c, event, msg, is_code, is_time, is_extend, is_stock, is_account, is_seller, is_help)
    except Exception as e:
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

def _process_buyer_command(c, event, msg, is_code, is_time, is_extend, is_stock, is_account, is_seller, is_help=False):
    author_name = getattr(msg, 'author', None) or getattr(msg, 'author_username', None)
    author_id = getattr(msg, 'author_id', None) or 0
    if is_help:
        _send_fp(c, msg.chat_id, (
            "━━━━━━━━━━━━━━━━━━━\n"
            "📋  ДОСТУПНЫЕ КОМАНДЫ\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            f"{BUYER_COMMANDS_TEXT}\n"
            "━━━━━━━━━━━━━━━━━━━"
        ))
        return
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
            lines = "\n".join(f"{i + 1}. {acc_o.login}" for i, (_, acc_o) in enumerate(targets))
            with _extend_account_choice_lock:
                _extend_account_choice_pending[msg.chat_id] = {
                    "order_ids": [o.id for o, _ in targets],
                    "expire": time.time() + EXTEND_ACCOUNT_CHOICE_TIMEOUT,
                }
            _send_fp(c, msg.chat_id,
                     f"У вас несколько активных аккаунтов. Напишите номер аккаунта, который хотите продлить:\n{lines}")
            return

        _process_extend_for_target(c, msg, order, acc)

def _process_extend_for_target(c, msg, order, acc):
    lot_cfg = SETTINGS.get_lot(order.lot_id) if order.lot_id else None

    effective_options = _effective_extend_options(lot_cfg) if lot_cfg else []
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
        _send_fp(c, msg.chat_id, SETTINGS.messages.extend_no_lot)
        return

    remaining = _remaining_str(acc.rental_end) if acc.rental_end else "—"
    link = _enable_extend_lot(c, order)
    _send_fp(c, msg.chat_id, _tmpl(SETTINGS.messages.extend_link, link=link, remaining=remaining))

def _handle_feedback_event(c, message):
    try:
        from FunPayAPI.common.utils import RegularExpressions
        oids = RegularExpressions().ORDER_ID.findall(message.text or "")
    except Exception:
        return
    if not oids:
        return
    oid = oids[0].replace("#", "")
    order = ORDERS.get(oid)
    if not order:
        return

    stars = None
    for attr in ("review_stars", "stars", "rating", "vote"):
        val = getattr(message, attr, None)
        if val is not None:
            try:
                stars = int(val)
            except Exception:
                pass
            break
    try:
        fp_order = c.account.get_order(oid)
        review = getattr(fp_order, "review", None)
        if review is not None:
            api_stars = getattr(review, "stars", None) or getattr(review, "rating", None)
            if api_stars is not None:
                stars = int(api_stars)
    except Exception as e:
        logger.debug(f"[ASRplus] #{oid}: не удалось проверить текущую оценку отзыва: {e}")

    if stars == 1:
        _auto_refund_1star(c, order, oid)
        return
    if stars is None or stars >= 5:
        _handle_feedback(c, message)

def _auto_refund_1star(c, order, oid):
    if not SETTINGS.auto_refund_1star:
        return
    if order.status in (RentStatus.FINISHED, RentStatus.REFUND):
        return
    try:
        elapsed = (_now() - _parse(order.created_at)).total_seconds()
    except Exception:
        elapsed = None
    if elapsed is None or elapsed >= ONE_STAR_REFUND_WINDOW_SECONDS:
        if tg_logs:
            mins = "?" if elapsed is None else f"{int(elapsed // 60)}"
            tg_logs.error(
                f"⭐️ Заказ #{oid}: отзыв 1⭐, но с начала аренды прошло уже "
                f"{mins} мин (порог {ONE_STAR_REFUND_WINDOW_SECONDS // 60} мин) — "
                f"автовозврат НЕ выполнен, нужно решение продавца вручную."
            )
        return
    with _data_lock:
        acc = AccountRepo.get(order.acc_id)
        same_rental = bool(
            acc and acc.id == order.acc_id and acc.owner_id == order.buyer_id
            and acc.status in (RentStatus.ACTIVE, RentStatus.FROZEN)
        )
    if not same_rental:
        if tg_logs:
            tg_logs.error(
                f"⭐️ Заказ #{oid}: отзыв 1⭐ в пределах "
                f"{ONE_STAR_REFUND_WINDOW_SECONDS // 60} мин, но аккаунт уже не "
                f"привязан к этому заказу/покупателю — автовозврат пропущен, "
                f"нужна проверка вручную."
            )
        return
    try:
        c.account.refund(order.id)
        refund_ok = True
    except Exception as e:
        refund_ok = False
        refund_err = _safe_err(e)
        logger.warning(f"[ASRplus] 1⭐-возврат #{oid}: FunPay отклонил возврат — {refund_err}")
    if not refund_ok:
        if tg_logs:
            tg_logs.error(
                f"⭐️ Заказ #{oid}: отзыв 1⭐, но возврат через FunPay не прошёл.\n"
                f"∟ Причина: {refund_err}\n"
                f"∟ Нужна проверка вручную."
            )
        return
    order.update(status=RentStatus.REFUND)
    if tg_logs:
        tg_logs.refund(order.id, f"Отзыв 1⭐ в первые {ONE_STAR_REFUND_WINDOW_SECONDS // 60} мин аренды")
    if order.chat_id:
        _send_fp(c, order.chat_id,
                 "💰 Оформлен автоматический возврат средств: аренда только началась, "
                 "и была поставлена низкая оценка. Если это ошибка или проблема с "
                 "аккаунтом — напишите продавцу.")

    def _reset_after_1star(a=acc, o=order):
        try:
            np = _change_password_with_retry(a)
            AccountRepo.release(a.id, np)
        except Exception as e:
            code, desc, _ = _classify_error(e)
            logger.error(f"[ASRplus] Автосброс после 1⭐-возврата: смена пароля не удалась {a.login} — [{code}] {desc}")
            AccountRepo.release(a.id, error=True)
            if tg_logs:
                tg_logs.error(
                    f"🔑 Возврат по отзыву 1⭐ (заказ #{o.id}) оформлен, но смену "
                    f"пароля выполнить не удалось: <b>{a.login}</b>\n∟ Причина: {desc}\n"
                    f"∟ Требуется сменить пароль вручную."
                )
    threading.Thread(target=_reset_after_1star, daemon=True).start()

def _handle_feedback(c, message):
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
    if not order:
        return
    msg_chat_id = getattr(message, "chat_id", None)
    if msg_chat_id is not None and order.chat_id != msg_chat_id:
        try:
            order.update(chat_id=int(msg_chat_id))
        except (TypeError, ValueError):
            pass
    if order.review_claimed:
        return
    try:
        fp_order = c.account.get_order(oid)
        review = getattr(fp_order, "review", None)
        if review is not None:
            review_stars = getattr(review, "stars", None) or getattr(review, "rating", None)
            if review_stars is not None and int(review_stars) < 5:
                logger.info(f"[ASRplus] #{oid}: отзыв найден, но не 5⭐ ({review_stars}) — бонус не начисляем")
                return
    except Exception as e:
        logger.debug(f"[ASRplus] #{oid}: не удалось проверить актуальность отзыва через API: {e}")
    bonus = SETTINGS.get_bonus_for_hours(order.hours)
    if bonus > 0:
        with _data_lock:
            acc = AccountRepo.get(order.acc_id)
            same_active_rental = bool(
                acc and acc.id == order.acc_id and acc.owner_id == order.buyer_id
                and acc.status in (RentStatus.ACTIVE, RentStatus.FROZEN)
            )
            acc_is_free = bool(acc and acc.status == RentStatus.FREE)
        if same_active_rental:
            ne = AccountRepo.extend_rent(order.acc_id, bonus)
        elif acc_is_free:
            ne = AccountRepo.reactivate_for_bonus(order, bonus)
        else:
            ne = None
            if tg_logs:
                tg_logs.error(
                    f"🎁 Бонус +{_fmt_hours(bonus)}ч по заказу #{oid} не начислен "
                    f"автоматически: аккаунт <b>{order.acc_login}</b> сейчас занят "
                    f"другим арендатором. Начислите вручную."
                )
        if ne:
            order.update(review_claimed=True, review_bonus_hours=bonus)
            _send_fp(c, order.chat_id, _tmpl(SETTINGS.messages.bonus, hours=str(bonus)))

def process_order_status_changed(c, event):
    if not SETTINGS.enabled or event.order.status not in (OrderStatuses.CLOSED, OrderStatuses.REFUNDED):
        return
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
        acc = AccountRepo.by_order(event.order.id) or AccountRepo.get(order.acc_id)
        rent_still_active = False
        if acc and acc.status == RentStatus.ACTIVE and acc.current_order == order.id:
            if acc.rental_end:
                rent_still_active = (_parse(acc.rental_end) - _now()).total_seconds() > 0
            else:
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

_extend_lot_timers: Dict[str, threading.Timer] = {}
_extend_lot_timers_lock = threading.Lock()

EXTEND_LOT_TIMEOUT = 300

def _schedule_extend_lot_disable(c, extend_lot_id: str, order_id: str = ""):
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
                            order.update(warned=True)
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
    ACC_FREEZE = "asr_frz"
    ACC_UNFREEZE = "asr_unfrz"
    ACC_REPLACE = "asr_repl"
    LOTS_BULK_DOWNLOAD = "asr_lots_bulk_dl"
    LOTS_BULK_UPLOAD = "asr_lots_bulk_up"
    LOTS_BULK_CONFIRM = "asr_lots_bulk_cf"
    CONFIRM_1STAR_REFUND = "asr_cf1star_prompt"
    ACC_SET_PWD = "asr_setpwd"
    ACC_EDIT_MAFILE = "asr_editma"
    LOTS = "asr_lots"
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
    STATS_RESET = "asr_strst"
    STATS_RESET_YES = "asr_strstyes"
    STATS_RESET_NO = "asr_strstno"
    FUNCTIONS = "asr_func"
    BLACKLIST = "asr_bl"
    BLACKLIST_ADD = "asr_bladd"
    BLACKLIST_DEL = "asr_bldel"
    ACC_SET_LIMIT = "asr_setlim"
    LOT_NOTE = "asr_lnote"
    LOT_HOURS_PER_UNIT = "asr_lhpu"
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
    LOT_HOURS_PER_UNIT = "ASR_LOT_HOURS_PER_UNIT"
    LOT_HOURS_PER_UNIT_ADD = "ASR_LOT_HOURS_PER_UNIT_ADD"
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
    BULK_LOTS_JSON = "ASR_BULK_LOTS_JSON"
    EXTEND_LOT_ID = "ASR_EXTEND_LOT_ID"
    EXTOPT_LOT_ID = "ASR_EXTOPT_LOT_ID"
    EXTOPT_HOURS = "ASR_EXTOPT_HOURS"

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

_logo_bytes_cache: Optional[bytes] = None

def _get_logo_bytes() -> Optional[bytes]:
    """Декодирует встроенный логотип ASR+ из base64 (один раз, с кэшем)."""
    global _logo_bytes_cache
    if _logo_bytes_cache is None:
        try:
            _logo_bytes_cache = base64.b64decode(ASR_LOGO_B64)
        except Exception as e:
            logger.warning(f"[ASRplus] Не удалось декодировать логотип: {e}")
            _logo_bytes_cache = b""
    return _logo_bytes_cache or None

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
        try:
            return c.data.split(":")[idx]
        except (IndexError, AttributeError):
            return ""

    def _pid(c, idx=-1):
        try:
            return int(_p(c, idx))
        except (ValueError, TypeError):
            logger.warning(f"[ASRplus] Некорректный callback_data (устаревшая кнопка?): {getattr(c, 'data', None)!r}")
            return 0

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
        if SETTINGS.auto_refund_1star:
            kb.row(B(f"{_is_on(True)} АВТО-ВОЗВРАТ 1 ЗВЕЗДА", None, f"{CBT.TOGGLE}:auto_refund_1star"))
        else:
            kb.row(B(f"{_is_on(False)} АВТО-ВОЗВРАТ 1 ЗВЕЗДА", None, CBT.CONFIRM_1STAR_REFUND))
        kb.add(B("⬅️ Назад", None, CBT.FUNCTIONS))
        return kb

    def confirm_1star_refund_prompt(c):
        answer(c)
        mins = ONE_STAR_REFUND_WINDOW_SECONDS // 60
        text = (
            "⭐️ <b>АВТО-ВОЗВРАТ 1 ЗВЕЗДА</b>\n\n"
            "Как это работает:\n"
            f"∟ Покупатель ставит отзыв 1⭐ в первые {mins} минут аренды → "
            "оформляется автоматический возврат средств, аккаунту меняется "
            "пароль, аренда прекращается.\n"
            f"∟ Если с начала аренды прошло больше {mins} минут → возврата "
            "НЕ будет, решение остаётся за продавцом вручную.\n\n"
            "Включить эту функцию?"
        )
        kb = K(row_width=2)
        kb.add(B("✅ Да, включить", None, f"{CBT.TOGGLE}:auto_refund_1star"),
               B("❌ Отмена", None, CBT.FUNCTIONS))
        edit(c.message, text, kb)

    def open_main(c):
        edit(c.message, _main_text(), _main_kb())

    def _send_main_with_logo(chat_id):
        logo = _get_logo_bytes()
        if logo:
            try:
                bot.send_photo(chat_id, ("asr_logo.jpg", logo),
                                caption=_main_text(), reply_markup=_main_kb(), parse_mode="HTML")
                return
            except Exception as e:
                logger.warning(f"[ASRplus] Не удалось отправить лого: {e}")
        send(chat_id, _main_text(), _main_kb())

    def open_main_cmd(m):
        _send_main_with_logo(m.chat.id)

    def open_about(c):
        answer(c)
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
                     "auto_free_on_error", "save_deleted_acc", "auto_refund_1star",
                     "notification_order_completed", "notification_error", "notification_refund",
                     "notification_preparing"):
            return answer(c, "❌ Недопустимое поле", True)
        SETTINGS.toggle(p)
        if p.startswith("notification"):
            open_notifs(c)
        elif p in ("auto_disable_lots", "auto_enable_lots", "autoback_on_error", "auto_free_on_error",
                   "save_deleted_acc", "auto_refund_1star"):
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
        parts = c.data.split(':')
        selected_tag = parts[1] if len(parts) > 1 and parts[1] else None
        tags = AccountRepo.all_tags()
        if not tags:
            return answer(c, '❌ Нет аккаунтов', True)
        if not selected_tag or selected_tag == '0':
            kb = K(row_width=2)
            for tag in sorted(tags):
                free = AccountRepo.count_free(tag).get(_ntag(tag), 0)
                total_tag = sum(1 for a in ACCOUNTS if _ntag(a.tag) == _ntag(tag))
                free_icon = "🟢" if free > 0 else "🔴"
                kb.add(B(f'🏷 {tag}  ({total_tag} акк / {free} {free_icon})', None, f'{CBT.ACC_BY_TAG}:{tag}'))
            kb.add(B('⬅️ Назад', None, CBT.ACC_MENU))
            edit(c.message, '<b>🏷 Сортировка по тегам</b>\n\nВыберите тег:', kb)
        else:
            accs = [a for a in ACCOUNTS if _ntag(a.tag) == _ntag(selected_tag)]
            try:
                pg = int(parts[2]) if len(parts) > 2 else 0
            except ValueError:
                pg = 0
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
        RentStatus.FROZEN: "На паузе",
    }

    def _acc_text(acc):
        icon = ICON_STATUS.get(acc.status, "❓")
        status_lbl = _STATUS_LABEL.get(acc.status, acc.status)
        if acc.status == RentStatus.FROZEN:
            status_lbl = "На паузе (был свободен)" if acc.frozen_from == RentStatus.FREE else "На паузе (аренда приостановлена)"
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
        if acc.status in (RentStatus.ACTIVE, RentStatus.FREE):
            kb.add(B("⏸ Заморозить", None, f"{CBT.ACC_FREEZE}:{acc.id}"))
        if acc.status == RentStatus.FROZEN:
            kb.add(B("▶️ Разморозить", None, f"{CBT.ACC_UNFREEZE}:{acc.id}"))
        if acc.status in (RentStatus.ACTIVE, RentStatus.FROZEN):
            kb.add(B("♻️ Заменить аккаунт", None, f"{CBT.ACC_REPLACE}:{acc.id}"))
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
            a = None
            try:
                a = AccountRepo.get(acc_id)
                if not a:
                    send(chat_id, "❌ Аккаунт не найден")
                    return
                np = _change_password_with_retry(a)
                AccountRepo.set_password_bot(a.id, np)
                send(chat_id, f"✅ Пароль <code>{a.login}</code> изменён:\n<code>{np}</code>")
            except Exception as e:
                code, desc, _ = _classify_error(e)
                login = a.login if a else str(acc_id)
                send(chat_id, f"❌ Не удалось сменить пароль <code>{login}</code>\n∟ [{code}] {desc}")
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

    def acc_freeze(c):
        aid = _pid(c)
        acc = AccountRepo.get(aid)
        if not acc:
            return answer(c, "❌ Не найден", True)
        was_free = acc.status == RentStatus.FREE
        if AccountRepo.freeze_account(aid):
            answer(c, "⏸ Аккаунт поставлен на паузу" if was_free else "⏸ Аренда заморожена")
            acc = AccountRepo.get(aid)
            edit(c.message, _acc_text(acc), _acc_kb(acc))
            order = ORDERS.get(acc.current_order) if acc.current_order else None
            if order:
                _send_fp(c, order.chat_id, "⏸ Ваша аренда временно приостановлена продавцом. "
                                            "Время аренды не расходуется.")
        else:
            answer(c, "❌ Заморозить можно только свободный или арендуемый аккаунт", True)

    def acc_unfreeze(c):
        aid = _pid(c)
        acc = AccountRepo.get(aid)
        if not acc:
            return answer(c, "❌ Не найден", True)
        was_free = acc.frozen_from == RentStatus.FREE
        if AccountRepo.unfreeze_account(aid):
            answer(c, "▶️ Аккаунт снят с паузы" if was_free else "▶️ Аренда возобновлена")
            acc = AccountRepo.get(aid)
            edit(c.message, _acc_text(acc), _acc_kb(acc))
            order = ORDERS.get(acc.current_order) if acc.current_order else None
            if order:
                remaining_str = _remaining_str(acc.rental_end) if acc.rental_end else "—"
                _send_fp(c, order.chat_id, _tmpl(
                    "▶️ Ваша аренда возобновлена. Осталось: $remaining",
                    remaining=remaining_str))
        else:
            answer(c, "❌ Не удалось разморозить (аккаунт не в заморозке)", True)

    def acc_replace(c):
        aid = _pid(c)
        old_acc, new_acc = AccountRepo.replace_account(aid)
        if not old_acc:
            return answer(c, "❌ Не найден", True)
        if not new_acc:
            return answer(c, f"❌ Нет свободных аккаунтов с тегом [{old_acc.tag}] для замены", True)
        answer(c, f"♻️ Заменено: {old_acc.login} → {new_acc.login}")
        order = ORDERS.get(new_acc.current_order) if new_acc.current_order else None
        if order:
            remaining_str = _remaining_str(new_acc.rental_end) if new_acc.rental_end else "—"
            _send_fp(c, order.chat_id, _tmpl(
                "♻️ Продавец заменил ваш аккаунт на новый (по техническим причинам).\n"
                "🔑 Логин: $login\n🔒 Пароль: $password\n⏳ Осталось: $remaining",
                login=new_acc.login, password=new_acc.password, remaining=remaining_str))
        if tg_logs:
            try:
                tg_logs.error(f"Аккаунт {old_acc.login} заменён на {new_acc.login} "
                              f"(заказ #{new_acc.current_order})")
            except Exception:
                pass
        edit(c.message, _acc_text(new_acc), _acc_kb(new_acc))

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
        ids = list(SETTINGS.lots.keys())
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
                        rate = _fmt_hours(lc.hours_per_unit)
                        kb.add(B(f"[{lc.tag}] {display}  ·  {rate}ч/шт  ·  {free} {free_icon}",
                                 None, f"{CBT.LOT_DETAIL}:{lid}"))
        kb.add(B("➕ Добавить лот", None, CBT.LOT_ADD))
        kb.row(B("🟢 Вкл все", None, CBT.LOTS_ENABLE_ALL), B("🔴 Выкл все", None, CBT.LOTS_DISABLE_ALL))
        if ids:
            kb.add(B("⬇️ Массовая выгрузка лотов (JSON)", None, CBT.LOTS_BULK_DOWNLOAD))
        kb.add(B("⬆️ Массовая загрузка лотов (JSON)", None, CBT.LOTS_BULK_UPLOAD))
        kb.add(B("🔄 Обновить", None, CBT.LOTS), B("⬅️ Назад", None, CBT.MAIN))
        text = f"<b>🔗 Лоты</b> — всего: <code>{len(ids)}</code>"
        if not ids:
            text += "\nЛоты не добавлены."
        edit(c.message, text, kb)

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
        # Инфо о лот-продлении (варианты + легаси одиночный лот)
        extend_info = ""
        if lc.extend_options:
            opt_lines = []
            for o in lc.extend_options:
                o_id = str(o.get("lot_id"))
                line = f"#{o_id} ({_fmt_hours(o.get('hours'))}ч/шт)"
                with _extend_lot_timers_lock:
                    if o_id in _extend_lot_timers:
                        line += " ⏱"
                opt_lines.append(line)
            extend_info = "\n∟ 🔄 Лот-продление: " + ", ".join(opt_lines)
        elif lc.extend_lot_id:
            extend_info = f"\n∟ 🔄 Лот-продление: <code>#{lc.extend_lot_id}</code>"
            with _extend_lot_timers_lock:
                if lc.extend_lot_id in _extend_lot_timers:
                    extend_info += " ⏱ <i>(ожидает покупки)</i>"
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
        rate_line = f"\n∟ ⏱ 1 шт = <code>{_fmt_hours(lc.hours_per_unit)}ч</code>"
        text = (
            f"🔗 <b>Лот #{lid}</b>\n\n"
            f"∟ 🏷 Тег (пул аккаунтов): <code>{lc.tag}</code>{rate_line}\n"
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
        kb.add(B(f"⏱ 1 шт = {_fmt_hours(lc.hours_per_unit)}ч ✏️", None, f"{CBT.LOT_HOURS_PER_UNIT}:{lid}"))
        # Кнопка лот-продление (единая система вариантов "часов за 1 шт" для всех лотов)
        n_opts = len(lc.extend_options)
        if n_opts:
            opt_label = f"🔄 Лот-продление ({n_opts} вариант(а))"
        elif lc.extend_lot_id:
            opt_label = f"🔄 Лот-продление: #{lc.extend_lot_id} ✏️"
        else:
            opt_label = "🔄 Лот-продление (не задан)"
        kb.add(B(opt_label, None, f"{CBT.LOT_EXTEND_LOT}:{lid}"))
        if fp_active is True:
            kb.add(B("🔴 Выключить", None, f"{CBT.LOT_TOGGLE_FP}:{lid}:0"))
        elif fp_active is False:
            kb.add(B("🟢 Включить", None, f"{CBT.LOT_TOGGLE_FP}:{lid}:1"))
        else:
            kb.add(B("⚡ Вкл/Выкл", None, f"{CBT.LOT_TOGGLE_FP}:{lid}:toggle"))
        kb.add(B("🗑 Удалить", None, f"{CBT.LOT_DEL_CONFIRM}:{lid}"))
        kb.add(B("⬅️ К списку", None, CBT.LOTS))
        edit(c.message, text, kb)

    def lots_bulk_download(c):
        answer(c)
        chat_id = c.message.chat.id
        ids = list(SETTINGS.lots.keys())
        if not ids:
            return answer(c, "❌ Нет настроенных лотов для выгрузки", True)
        export_data = []
        for lid in ids:
            lc = SETTINGS.get_lot(lid)
            if not lc:
                continue
            export_data.append({
                "lot_id": lid,
                "tag": lc.tag,
                "note": lc.note,
                "hours_per_unit": lc.hours_per_unit,
                "extend_lot_id": lc.extend_lot_id,
                "extend_options": lc.extend_options,
                "subcategory_id": lc.subcategory_id,
            })
        content = json.dumps(export_data, indent=2, ensure_ascii=False, default=str)
        data_bytes = content.encode("utf-8")
        now_str = _fmt(_now()).replace(":", "-").replace(" ", "_")
        filename = f"lots_bulk_{now_str}.json"
        try:
            bot.send_document(
                chat_id,
                (filename, data_bytes),
                caption=(
                    f"📦 <b>{filename}</b>\n"
                    f"∟ Лотов: {len(export_data)}\n"
                    f"∟ Содержит: lot_id, tag, note, hours_per_unit, "
                    f"extend_lot_id, extend_options, subcategory_id"
                ),
                parse_mode="HTML"
            )
            logger.info(f"[ASRplus] Выгружено {len(export_data)} лотов в {filename}")
        except Exception as e:
            logger.warning(f"[ASRplus] Ошибка выгрузки лотов: {e}")
            bot.send_message(chat_id, f"❌ Не удалось отправить файл: {_safe_err(e)}", parse_mode="HTML")

    def lots_bulk_upload_start(c):
        answer(c)
        _temp_storage.setdefault(c.from_user.id, {})
        _ask(
            c.message.chat.id, c.from_user.id, States.BULK_LOTS_JSON,
            "📤 <b>Загрузка лотов</b>\n\n"
            "Отправьте файл <code>lots_bulk_*.json</code> (полученный через "
            "«Массовая выгрузка лотов»).\n\n"
            "Формат файла:\n"
            "<pre>[\n"
            "  {\n"
            '    "lot_id": "12345",\n'
            '    "tag": "default",\n'
            '    "hours_per_unit": 1.0,\n'
            '    "note": "...",\n'
            '    "extend_lot_id": null,\n'
            '    "extend_options": [],\n'
            '    "subcategory_id": null\n'
            "  },\n"
            "  ...\n"
            "]</pre>\n\n"
            "⚠️ Лоты с ID, которые уже есть в базе, будут пропущены "
            "(перезаписи существующих не будет).",
            _back_kb(CBT.LOTS)
        )

    def _h_lots_bulk_upload(m):
        if not tg.check_state(m.chat.id, m.from_user.id, States.BULK_LOTS_JSON):
            return
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        content = None
        if m.document:
            try:
                fi = bot.get_file(m.document.file_id)
                raw = bot.download_file(fi.file_path)
                content = raw.decode("utf-8")
            except Exception as e:
                send(m.chat.id, f"❌ Ошибка чтения файла: {_safe_err(e)}", _back_kb(CBT.LOTS))
                return
        elif m.text:
            content = m.text.strip()

        if not content:
            send(m.chat.id, "❌ Отправьте файл lots_bulk.json или JSON текст", _back_kb(CBT.LOTS))
            return

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            send(m.chat.id, f"❌ Невалидный JSON: {_safe_err(e)}", _back_kb(CBT.LOTS))
            return

        if not isinstance(data, list):
            send(m.chat.id, "❌ Ожидается список (массив) лотов", _back_kb(CBT.LOTS))
            return
        if not data:
            send(m.chat.id, "❌ Файл пустой (нет лотов)", _back_kb(CBT.LOTS))
            return

        errors = []
        valid = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                errors.append(f"[{i+1}] не словарь")
                continue
            lot_id = str(item.get("lot_id") or "").strip()
            tag = (item.get("tag") or "").strip()
            if not lot_id:
                errors.append(f"[{i+1}] нет поля lot_id")
                continue
            if not tag:
                errors.append(f"[{i+1}] {lot_id}: нет поля tag")
                continue
            try:
                hours_per_unit = float(item.get("hours_per_unit") or 1.0)
            except (TypeError, ValueError):
                hours_per_unit = 1.0
            valid.append({
                "lot_id": lot_id,
                "tag": _ntag(tag),
                "note": item.get("note"),
                "hours_per_unit": hours_per_unit,
                "extend_lot_id": item.get("extend_lot_id"),
                "extend_options": item.get("extend_options") or [],
                "subcategory_id": item.get("subcategory_id"),
            })

        if not valid:
            err_lines = "\n".join(errors[:15])
            send(m.chat.id, f"❌ Нет валидных лотов.\n\nОшибки:\n{err_lines}", _back_kb(CBT.LOTS))
            return

        _temp_storage.setdefault(m.from_user.id, {})["lots_bulk_data"] = valid
        skip_existing = sum(1 for v in valid if SETTINGS.has_lot(v["lot_id"]))
        to_add = len(valid) - skip_existing

        warn_text = ""
        if errors:
            warn_text = f"\n⚠️ Пропущено с ошибками: {len(errors)}"
        if skip_existing:
            warn_text += f"\n⚠️ Уже существуют (будут пропущены): {skip_existing}"

        kb = K(row_width=2)
        kb.add(B("✅ Добавить", None, f"{CBT.LOTS_BULK_CONFIRM}:yes"),
               B("❌ Отмена", None, CBT.LOTS))
        send(m.chat.id,
             f"📦 <b>Подтверждение загрузки лотов</b>\n\n"
             f"∟ Всего в файле: <b>{len(data)}</b>\n"
             f"∟ Будет добавлено: <b>{to_add}</b>{warn_text}\n\n"
             f"Продолжить?",
             kb)

    def lots_bulk_confirm(c):
        action = _p(c)
        answer(c)
        if action != "yes":
            open_lots(c)
            return
        data = _temp_storage.get(c.from_user.id, {}).get("lots_bulk_data", [])
        if not data:
            return answer(c, "❌ Данные утеряны", True)

        added = []
        skipped = []
        for item in data:
            lid = item["lot_id"]
            if SETTINGS.has_lot(lid):
                skipped.append(lid)
                continue
            SETTINGS.set_lot(lid, item["tag"], extend_lot_id=item["extend_lot_id"],
                              note=item["note"], subcategory_id=item["subcategory_id"],
                              hours_per_unit=item["hours_per_unit"])
            for opt in item["extend_options"]:
                try:
                    SETTINGS.add_lot_extend_option(lid, str(opt["lot_id"]), float(opt["hours"]))
                except (KeyError, TypeError, ValueError):
                    pass
            SETTINGS.ensure_match_tag(lid)
            added.append(lid)

        _invalidate_lots_cache()

        edit(c.message,
             f"✅ <b>Загрузка завершена</b>\n\n"
             f"∟ Добавлено: <b>{len(added)}</b>\n"
             f"∟ Пропущено (уже были): <b>{len(skipped)}</b>",
             _back_kb(CBT.LOTS))

        if added and cardinal_ref:
            def _write_ids(lids=list(added)):
                ok_count = 0
                for lid in lids:
                    try:
                        ok, _mtag, _msg = _auto_write_match_tag(cardinal_ref, str(lid))
                        if ok:
                            ok_count += 1
                    except Exception as e:
                        logger.debug(f"[ASRplus] Автозапись ID лота {lid}: {e}")
                    time.sleep(1)
                if tg_logs:
                    tg_logs.error(
                        f"📦 Массовая загрузка лотов: служебный ID дописан в описание "
                        f"{ok_count}/{len(lids)} лотов. Остальным — вручную кнопкой «🏷 Авто ID»."
                    )
            threading.Thread(target=_write_ids, daemon=True).start()

        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        lot_url = FUNPAY_LOT_URL.format(lot_id=lid)
        free_count = AccountRepo.count_free(lc.tag).get(_ntag(lc.tag), 0)
        fp_active = None
        try:
            lf = cardinal_ref.account.get_lot_fields(int(lid))
            if lf is not None and lf.active is not None:
                fp_active = bool(lf.active)
        except Exception:
            try:
                fp_lots = _get_cached_lots(cardinal_ref)
                fp_lot = next((l for l in fp_lots if str(l.id) == lid), None)
                if fp_lot and fp_lot.active is not None:
                    fp_active = bool(fp_lot.active)
            except Exception:
                pass
        active_str = "🟢 Включён" if fp_active is True else ("🔴 Выключен" if fp_active is False else "⚪ Нет данных")
        note_str = f"\n∟ 📝 Заметка: <code>{lc.note}</code>" if lc.note else ""
        extend_info = ""
        if lc.extend_options:
            opt_lines = []
            for o in lc.extend_options:
                o_id = str(o.get("lot_id"))
                line = f"#{o_id} ({_fmt_hours(o.get('hours'))}ч/шт)"
                with _extend_lot_timers_lock:
                    if o_id in _extend_lot_timers:
                        line += " ⏱"
                opt_lines.append(line)
            extend_info = "\n∟ 🔄 Лот-продление: " + ", ".join(opt_lines)
        elif lc.extend_lot_id:
            extend_info = f"\n∟ 🔄 Лот-продление: <code>#{lc.extend_lot_id}</code>"
            with _extend_lot_timers_lock:
                if lc.extend_lot_id in _extend_lot_timers:
                    extend_info += " ⏱ <i>(ожидает покупки)</i>"
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
        rate_line = f"\n∟ ⏱ 1 шт = <code>{_fmt_hours(lc.hours_per_unit)}ч</code>"
        text = (
            f"🔗 <b>Лот #{lid}</b>\n\n"
            f"∟ 🏷 Тег (пул аккаунтов): <code>{lc.tag}</code>{rate_line}\n"
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
        kb.add(B(f"⏱ 1 шт = {_fmt_hours(lc.hours_per_unit)}ч ✏️", None, f"{CBT.LOT_HOURS_PER_UNIT}:{lid}"))
        n_opts = len(lc.extend_options)
        if n_opts:
            opt_label = f"🔄 Лот-продление ({n_opts} вариант(а))"
        elif lc.extend_lot_id:
            opt_label = f"🔄 Лот-продление: #{lc.extend_lot_id} ✏️"
        else:
            opt_label = "🔄 Лот-продление (не задан)"
        kb.add(B(opt_label, None, f"{CBT.LOT_EXTEND_LOT}:{lid}"))
        if fp_active is True:
            kb.add(B("🔴 Выключить", None, f"{CBT.LOT_TOGGLE_FP}:{lid}:0"))
        elif fp_active is False:
            kb.add(B("🟢 Включить", None, f"{CBT.LOT_TOGGLE_FP}:{lid}:1"))
        else:
            kb.add(B("⚡ Вкл/Выкл", None, f"{CBT.LOT_TOGGLE_FP}:{lid}:toggle"))
        kb.add(B("🗑 Удалить", None, f"{CBT.LOT_DEL_CONFIRM}:{lid}"))
        kb.add(B("⬅️ К списку", None, CBT.LOTS))
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
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        answer(c)
        return _open_extopt_list(c, lid)

    def _open_extopt_list(c, lid):
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
            lines.append(f"∟ <code>{_fmt_hours(o.get('hours'))}ч</code>/шт → лот <code>#{o_id}</code>{timer_mark}")
        opts_str = "\n".join(lines) if lines else "<i>вариантов пока нет</i>"
        legacy_str = ""
        if lc.extend_lot_id:
            legacy_str = f"\n\n<i>Старая привязка (легаси): #{lc.extend_lot_id}, 1 шт = 1ч</i>"
        text = (
            f"🔄 <b>Лоты-продления для лота #{lid}</b>\n\n"
            f"∟ Тег: <code>{lc.tag}</code>\n\n"
            f"{opts_str}{legacy_str}\n\n"
            f"<b>Как работает:</b>\n"
            f"1. Покупатель пишет <code>!продлить</code>\n"
            f"2. Плагин присылает список доступных вариантов времени продления\n"
            f"3. Покупатель пишет число часов в чат — плагин включает нужный лот и присылает ссылку\n"
            f"4. Если покупает несколько штук — время умножается (например 12ч=1шт, купил 2шт → 24ч)\n"
            f"5. Если не оплачивает за 5 минут — лот выключается автоматически"
        )
        kb = K(row_width=1)
        for o in opts:
            o_id = str(o.get("lot_id"))
            kb.add(B(f"🗑 Удалить {_fmt_hours(o.get('hours'))}ч/шт (#{o_id})", None, f"{CBT.LOT_EXTOPT_DEL}:{lid}:{o_id}"))
        if lc.extend_lot_id:
            kb.add(B(f"🗑 Удалить старую привязку (#{lc.extend_lot_id})", None, f"{CBT.LOT_EXTEND_LOT_DEL}:{lid}"))
        kb.add(B("➕ Добавить вариант", None, f"{CBT.LOT_EXTOPT_ADD}:{lid}"))
        kb.add(B("⬅️ Назад", None, f"{CBT.LOT_DETAIL}:{lid}"))
        edit(c.message, text, kb)

    def lot_extopt_add(c):
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
                target_tag = SETTINGS.gen_new_service_tag()
                ok, tag_msg = (False, "Не удалось создать новый ID")
                if target_tag:
                    ok, tag_msg = _write_tag_to_funpay_lot(cardinal_ref, ext_id, target_tag)
                    if ok:
                        SETTINGS.set_lot_extend_option_write_tag(main_id, ext_id, target_tag)
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
        parts = c.data.split(":")
        lid, ext_id = parts[1], parts[2]
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        SETTINGS.remove_lot_extend_option(lid, ext_id)
        _invalidate_lots_cache()
        threading.Thread(target=lambda: _unlink_extend_lot(cardinal_ref, ext_id), daemon=True).start()
        answer(c, f"✅ Вариант #{ext_id} удалён и отключён на FunPay")
        _open_extopt_list(c, lid)

    def lot_extend_lot_set(c):
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
        old_extend_id = (SETTINGS.get_lot(main_lot_id).extend_lot_id or None)
        SETTINGS.set_lot(main_lot_id,
                         SETTINGS.get_lot(main_lot_id).tag,
                         extend_lot_id=extend_id,
                         note=SETTINGS.get_lot(main_lot_id).note)
        _invalidate_lots_cache()
        msg_sent = send(
            m.chat.id,
            f"⏳ Привязываю лот-продление <code>#{extend_id}</code> к лоту <code>#{main_lot_id}</code>...")

        def _write_tag_bg(mid=m.chat.id, main_id=main_lot_id, ext_id=extend_id, prev_msg=msg_sent,
                          old_ext_id=old_extend_id):
            if old_ext_id and _ntag(old_ext_id) != _ntag(ext_id):
                _unlink_extend_lot(cardinal_ref, old_ext_id)
            try:
                target_tag = SETTINGS.gen_new_service_tag()
                ok, tag_msg = (False, "Не удалось создать новый ID")
                if target_tag:
                    ok, tag_msg = _write_tag_to_funpay_lot(cardinal_ref, ext_id, target_tag)
                    if ok:
                        SETTINGS.set_lot_extend_write_tag(main_id, target_tag)
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
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        if not lc.extend_lot_id:
            return answer(c, "ℹ️ Лот-продление не задан", True)
        old_extend = lc.extend_lot_id
        SETTINGS.set_lot(lid, lc.tag, extend_lot_id=None, note=lc.note)
        SETTINGS.set_lot_extend_write_tag(lid, None)
        _invalidate_lots_cache()
        threading.Thread(target=lambda: _unlink_extend_lot(cardinal_ref, old_extend), daemon=True).start()
        answer(c, f"✅ Лот-продление #{old_extend} отвязан и отключён на FunPay")
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
        SETTINGS.del_lot(lid)
        _invalidate_lots_cache()
        answer(c, f"✅ Лот {name} удалён")
        open_lots(c)

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
        _ask(c.message.chat.id, c.from_user.id, States.LOT_ID,
             "Введите <b>ID лота</b> или ссылку на лот:", _back_kb(CBT.LOTS))

    def _h_lot_id(m):
        raw = (m.text or "").strip()
        _cleanup_dialog(m.chat.id, m.from_user.id, m.message_id)
        lot_id = _extract_lot_id(raw)
        if not lot_id:
            send(m.chat.id, "❌ Не удалось распознать ID.", _back_kb(CBT.LOTS))
            return
        if SETTINGS.has_lot(lot_id):
            send(m.chat.id, f"❌ Лот <code>{lot_id}</code> уже добавлен", _back_kb(CBT.LOTS))
            return
        tags = AccountRepo.all_tags()
        if not tags:
            send(m.chat.id, "❌ Сначала добавьте аккаунты!", _main_kb())
            return
        _temp_storage.setdefault(m.from_user.id, {})["lot_id"] = lot_id
        kb = K()
        for tag in tags:
            kb.add(B(tag, None, f"{CBT.LOT_TAG}:{tag}"))
        kb.add(B("⬅️ Назад", None, CBT.LOTS))
        send(m.chat.id, "Выберите <b>тег</b> для лота:", kb)

    def _finalize_lot_add(chat_id, lid, tag, sub_id, hours_per_unit=1.0, edit_msg=None):
        SETTINGS.set_lot(str(lid), tag, subcategory_id=sub_id, hours_per_unit=hours_per_unit)
        _invalidate_lots_cache()
        ok, mtag, msg = _auto_write_match_tag(cardinal_ref, str(lid))
        if ok:
            id_line = f"\n🆔 ID лота: <code>#{mtag}</code> (автоматически добавлен в описание)"
        else:
            id_line = (
                f"\n⚠️ Не удалось автоматически дописать ID в описание лота ({msg}). "
                f"Откройте лот и нажмите «🏷 Авто ID» вручную."
            )
        hours_line = f"\n⏱ 1 шт = <code>{_fmt_hours(hours_per_unit)}ч</code>"
        text = f"✅ Лот {lid} привязан к тегу <code>{tag}</code>{hours_line}{id_line}"
        if edit_msg is not None:
            edit(edit_msg, text, _back_kb(CBT.LOTS))
        else:
            send(chat_id, text, _back_kb(CBT.LOTS))

    def lot_tag(c):
        tag = _ntag(_p(c))
        uid = c.from_user.id
        lid = _temp_storage.get(uid, {}).get("lot_id")
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
        _temp_storage[uid]["add_lot_tag"] = tag
        _temp_storage[uid]["add_lot_sub_id"] = sub_id
        answer(c)
        _ask(c.message.chat.id, uid, States.LOT_HOURS_PER_UNIT_ADD,
             f"📌 Лот <code>{lid}</code> привязан к тегу <code>{tag}</code>.\n\n"
             f"⏱ Укажите, <b>сколько часов аренды даёт 1 купленная штука</b> "
             f"этого лота (например: 1 — если 1 шт = 1 час, или 12 — если "
             f"1 шт = 12 часов).\n"
             f"<i>Если купят несколько штук, время умножится (12ч × 2шт = 24ч). "
             f"Значение можно изменить позже в настройках лота.</i>",
             _back_kb(CBT.LOTS))

    def _h_lot_hours_per_unit_add(m):
        uid = m.from_user.id
        d = _temp_storage.get(uid, {})
        lid = d.get("lot_id")
        tag = d.get("add_lot_tag")
        sub_id = d.get("add_lot_sub_id")
        _cleanup_dialog(m.chat.id, uid, m.message_id)
        if not lid or not tag:
            send(m.chat.id, "❌ Данные утеряны", _back_kb(CBT.LOTS))
            return
        raw = (m.text or "").strip().replace(",", ".")
        try:
            hours = float(raw)
            if hours <= 0:
                raise ValueError
        except (ValueError, TypeError):
            send(m.chat.id, "❌ Введите положительное число часов (например: 1 или 12)",
                 _back_kb(CBT.LOTS))
            return
        _finalize_lot_add(m.chat.id, lid, tag, sub_id, hours_per_unit=hours)

    def lot_hours_per_unit(c):
        lid = _p(c)
        lc = SETTINGS.get_lot(lid)
        if not lc:
            return answer(c, "❌ Лот не найден", True)
        _temp_storage.setdefault(c.from_user.id, {})["hpu_lot_id"] = lid
        answer(c)
        cur = _fmt_hours(lc.hours_per_unit)
        _ask(c.message.chat.id, c.from_user.id, States.LOT_HOURS_PER_UNIT,
             f"⏱ <b>Часов за 1 шт для лота #{lid}</b>\n\n"
             f"Текущее: <code>{cur}ч</code> за 1 шт\n\n"
             f"Введите новое значение (например: 1, 12 или 12.5):",
             _back_kb(f"{CBT.LOT_DETAIL}:{lid}"))

    def _h_lot_hours_per_unit(m):
        uid = m.from_user.id
        lid = (_temp_storage.get(uid) or {}).get("hpu_lot_id")
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
            send(m.chat.id, "❌ Введите положительное число часов (например: 1 или 12)",
                 _back_kb(f"{CBT.LOT_DETAIL}:{lid}"))
            return
        SETTINGS.set_lot_hours_per_unit(lid, hours)
        _invalidate_lots_cache()
        send(m.chat.id, f"✅ Для лота <code>{lid}</code> установлено: 1 шт = <code>{_fmt_hours(hours)}ч</code>",
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
        kb.add(B("🗑 Сбросить статистику", None, f"{CBT.STATS_RESET}:confirm"))
        kb.add(B("⬅️ Назад", None, CBT.MAIN))
        edit(c.message, _stats_text(), kb)

    def stats_reset_confirm(c):
        answer(c)
        finished_count = sum(1 for o in ORDERS.values()
                             if o.status in (RentStatus.FINISHED, RentStatus.REFUND, RentStatus.ERROR))
        kb = K(row_width=2)
        kb.add(B("✅ Да, сбросить", None, f"{CBT.STATS_RESET_YES}:do"),
               B("❌ Отмена", None, CBT.STATS))
        edit(c.message,
             f"⚠️ <b>Сбросить статистику?</b>\n\n"
             f"Будет удалено <b>{finished_count}</b> завершённых/отменённых заказов из статистики "
             f"(история и счётчики аренд/продлений/возвратов).\n"
             f"<i>Активные аренды не затрагиваются.</i>",
             kb)

    def stats_reset_do(c):
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
        logger.info(f"[ASRplus] Статистика сброшена: удалено {count} записей")
        kb = K(row_width=1)
        kb.add(B("⬅️ Назад", None, CBT.STATS))
        edit(c.message, f"✅ <b>Статистика сброшена</b>\n\nУдалено записей: <b>{count}</b>", kb)

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
    tg.cbq_handler(lots_bulk_download, lambda c: c.data == CBT.LOTS_BULK_DOWNLOAD)
    tg.cbq_handler(lots_bulk_upload_start, lambda c: c.data == CBT.LOTS_BULK_UPLOAD)
    tg.cbq_handler(lots_bulk_confirm, lambda c: c.data.split(":")[0] == CBT.LOTS_BULK_CONFIRM)
    tg.cbq_handler(confirm_1star_refund_prompt, lambda c: c.data == CBT.CONFIRM_1STAR_REFUND)
    tg.cbq_handler(open_acc_by_tag, lambda c: c.data == CBT.ACC_BY_TAG or c.data.startswith(f"{CBT.ACC_BY_TAG}:"))
    tg.cbq_handler(acc_search_start, lambda c: c.data == CBT.ACC_SEARCH)
    tg.cbq_handler(start_add, lambda c: c.data == CBT.ACC_ADD)
    tg.cbq_handler(open_lots, lambda c: c.data == CBT.LOTS)
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
        (CBT.ACC_FREEZE, acc_freeze), (CBT.ACC_UNFREEZE, acc_unfreeze),
        (CBT.ACC_REPLACE, acc_replace),
        (CBT.ACC_MANUAL, acc_manual_start), (CBT.ACC_MANUAL_HOURS, lambda c: None),
        (CBT.ACC_DEL_CONFIRM, acc_del_confirm), (CBT.ACC_DEL_YES, acc_del_yes),
        (CBT.ACC_DEL_NO, acc_del_no),
        (CBT.LOT_DETAIL, open_lot_detail), (CBT.LOT_EDIT, lot_edit),
        (CBT.LOT_EDIT_TAG, lot_edit_tag),
        (CBT.LOT_RENAME, lot_rename),
        (CBT.LOT_NOTE, lot_note),
        (CBT.LOT_HOURS_PER_UNIT, lot_hours_per_unit),
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
        (CBT.STATS_RESET, stats_reset_confirm), (CBT.STATS_RESET_YES, stats_reset_do),
        (CBT.STATS_RESET_NO, open_stats),
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
        (States.LOT_HOURS_PER_UNIT, _h_lot_hours_per_unit),
        (States.LOT_HOURS_PER_UNIT_ADD, _h_lot_hours_per_unit_add),
        (States.REV_HRS_CUSTOM, _h_rev_hrs_custom),
        (States.REV_BON_CUSTOM, _h_rev_bon_custom),
        (States.EXTEND_LOT_ID, _h_extend_lot_id),
        (States.EXTOPT_LOT_ID, _h_extopt_lot_id),
        (States.EXTOPT_HOURS, _h_extopt_hours),
    ]:
        tg.msg_handler(handler, func=lambda m, s=state: tg.check_state(m.chat.id, m.from_user.id, s))
    tg.msg_handler(_h_mafile, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, States.MAFILE))
    tg.msg_handler(_h_mafile_edit, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, States.EDIT_MAFILE))
    tg.msg_handler(_h_acc_search, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, States.ACC_SEARCH))
    tg.msg_handler(_h_blacklist_add, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, States.BLACKLIST_ADD))
    tg.msg_handler(_h_set_limit, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, States.SET_LIMIT))
    tg.msg_handler(_h_bulk_mafile, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, States.BULK_MAFILE))
    tg.msg_handler(_h_lots_bulk_upload, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, States.BULK_LOTS_JSON))
    try:
        tg.file_handler(States.MAFILE, _h_mafile)
        tg.file_handler(States.EDIT_MAFILE, _h_mafile_edit)
        tg.file_handler(States.BULK_MAFILE, _h_bulk_mafile)
        tg.file_handler(States.BULK_LOTS_JSON, _h_lots_bulk_upload)
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

    try:
        _welcome_text = (
            "╔══════════════════════╗\n"
            "║   <b>⚡ ASR+ v1.2.0</b>         ║\n"
            "╚══════════════════════╝\n\n"
            "✅ Плагин успешно загружен и готов к работе!\n\n"
            "👤 Разработчик: <b>@DzhantDev</b>\n"
            "📢 Канал: <a href=\"https://t.me/DzhantDev\">t.me/DzhantDev</a>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>© ASR+ by @DzhantDev — автоматическая аренда Steam</i>"
        )
        _logo = _get_logo_bytes()
        for uid in tg.authorized_users:
            try:
                if _logo:
                    bot.send_photo(uid, ("asr_logo.jpg", _logo), caption=_welcome_text, parse_mode="HTML")
                else:
                    raise RuntimeError("no logo")
            except Exception:
                bot.send_message(uid, _welcome_text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        pass

def cleanup(card: Cardinal):
    _stop_event.set()

BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [process_new_order]
BIND_TO_NEW_MESSAGE = [process_message]
BIND_TO_ORDER_STATUS_CHANGED = [process_order_status_changed]
BIND_TO_DELETE = cleanup
