"""値の整形：和暦、度分秒、全角半角、住所の分割など。

NFKC 正規化は住所・電話番号など数字を含む欄だけにかける。
説明文に NFKC をかけると「…」や「／」まで変わってしまうため（草加市の作業で実際に起きた）。
"""

from __future__ import annotations

import re
import unicodedata

ERAS = {"明治": 1867, "大正": 1911, "昭和": 1925, "平成": 1988, "令和": 2018}
KANJI_DIGITS = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
HYPHENS = "‐‑‒–—―−ー－ｰ"


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip()


def normalize_phone(s: str) -> str:
    s = nfkc(s)
    for h in HYPHENS:
        s = s.replace(h, "-")
    return s


def wareki_to_iso(s: str) -> str | None:
    """「昭和48年3月1日」「平成元年4月1日」→「1973-03-01」。変換できなければ None。"""
    m = re.search(r"(明治|大正|昭和|平成|令和)\s*(元|\d+)\s*[年.．/]\s*(\d+)\s*[月.．/]\s*(\d+)\s*日?", nfkc(s))
    if not m:
        return None
    year = ERAS[m.group(1)] + (1 if m.group(2) == "元" else int(m.group(2)))
    return f"{year:04d}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"


def to_decimal_degree(s: str) -> float | None:
    """10進数ならそのまま、度分秒（35°49′30.1″ / 35度49分30.1秒）なら10進数に。"""
    t = nfkc(s)
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        pass
    nums = re.findall(r"\d+(?:\.\d+)?", t)
    if len(nums) == 3:
        d, m, sec = (float(x) for x in nums)
        return round(d + m / 60 + sec / 3600, 7)
    return None


def split_count(s: str) -> tuple[str, str]:
    """「1棟」「3点」→ ("1", "棟")。数が取れなければ ("", 元の値)。"""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(.*)", nfkc(s))
    return (m.group(1), m.group(2)) if m else ("", s)


def kanji_number(s: str) -> int | None:
    """「十二」「二十」程度の漢数字を整数に（丁目用）。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, _, b = s.partition("十")
        tens = KANJI_DIGITS.get(a, 1) if a else 1
        ones = KANJI_DIGITS.get(b, 0) if b else 0
        return tens * 10 + ones
    if all(c in KANJI_DIGITS for c in s):
        return int("".join(str(KANJI_DIGITS[c]) for c in s))
    return None


def canonical_address(s: str) -> str:
    """比較用の住所表記：NFKC、ハイフン統一、漢数字の丁目を算用数字、「番地」「番」「号」をハイフンに。"""
    t = nfkc(s).replace(" ", "")
    for h in HYPHENS:
        t = t.replace(h, "-")
    t = re.sub(r"([〇一二三四五六七八九十]+)丁目", lambda m: f"{kanji_number(m.group(1))}丁目", t)
    t = re.sub(r"(\d+)番地?(?=\d)", r"\1-", t)
    t = re.sub(r"(\d+)号", r"\1", t)
    t = re.sub(r"(\d+)番地?$", r"\1", t)
    return t


def strip_city(address: str, prefecture: str, city: str) -> str:
    t = address
    for prefix in (prefecture + city, city):
        if t.startswith(prefix):
            return t[len(prefix):]
    return t


PREFECTURE_RE = re.compile(r"^(東京都|北海道|京都府|大阪府|.{2,3}?県)")


def outside_city(address: str, prefecture: str, city: str) -> bool:
    """住所がほかの都道府県・市区町村のものか（市が市外に持つ施設：臨海学園・少年自然の家など）。"""
    t = nfkc(address)
    m = PREFECTURE_RE.match(t)
    if m and m.group(1) != prefecture:
        return True
    return t.startswith(prefecture) and not t.startswith(prefecture + city)


def full_address(address: str, prefecture: str, city: str) -> str:
    """都道府県・市区町村が抜けていれば補う。"""
    t = nfkc(address)
    if t.startswith(prefecture):
        return t
    if t.startswith(city):
        return prefecture + t
    return prefecture + city + t


ADDRESS_RE = re.compile(
    r"^(?P<town>[^\d]+(?:\d+丁目)?)"          # 町字（〇丁目まで）
    r"(?P<num>\d+(?:[-−]\d+)*(?:番地?\d*)?(?:号)?(?:[-−]\d+)*)?"  # 番地以下
    r"\s*(?P<bldg>.*)$"                       # 建物名等（方書）
)


def split_address(address: str, prefecture: str, city: str) -> dict | None:
    """所在地を 町字／番地以下／建物名等（方書） に分ける。自信がなければ None（AIレビューへ回す）。"""
    rest = strip_city(canonical_address(address), prefecture, city)
    if not rest:
        return None
    # 番地のない住所（「中町」「大竹地内」）は町字だけ（＋「地内」）として決まった形に分ける
    m_town = re.fullmatch(r"([^\d()（）]+?)(地内|付近|地先)?", rest)
    if m_town and not re.search(r"[()（）、,]", rest):
        return {"所在地_町字": m_town.group(1), "所在地_番地以下": m_town.group(2) or "", "建物名等": ""}
    m = ADDRESS_RE.match(rest)
    if not m:
        return None
    town, num, bldg = m.group("town"), m.group("num") or "", m.group("bldg").strip()
    # 「地内」「付近」や、建物名に数字が残る・番地が取れないものは判断を人（AI）に回す
    if any(w in rest for w in ("地内", "付近", "地先")) or not num:
        return None
    if bldg in ("番", "他", "外", "号"):
        num, bldg = num + bldg, ""
    return {"所在地_町字": town, "所在地_番地以下": num, "建物名等": bldg}


def join_split(split: dict) -> str:
    return f"{split.get('所在地_町字', '')}{split.get('所在地_番地以下', '')}{split.get('建物名等', '')}"
