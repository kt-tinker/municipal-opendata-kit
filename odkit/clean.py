"""すでに標準形式で公開されているデータの「表記の崩れ」を直す。

越谷市のデータで実際に見つかったもの：
- 所在地_市区町村 に住所全体が入っている（「越谷市下間久里792-1」）
- 法人番号が Excel の指数表記で桁を失っている（「2.03E+12」）→ 直せないので空欄にして記録
- 日付が Excel のシリアル値（「42599」）、時刻が 1日を1とした小数（「0.25」）
- 番地の区切りが半角カナの長音（「6ｰ1」）や全角数字
- 電話番号が「(048)970-7411」

川口市のデータで見つかったもの：
- 日付が「2018/12/25」、時刻が「9:00」
- 電話番号が「(048)-259-7651」、先頭に空白

直した内容は (列, 直す前, 直した後, 理由) として返し、README とログに残す。
"""

from __future__ import annotations

import datetime as dt
import re

from .normalize import HYPHENS, PREFECTURE_RE, nfkc, normalize_phone

DATE_COLUMNS = ("文化財指定日",)
TIME_COLUMNS = ("開始時間", "終了時間")
NUMBER_HYPHEN = re.compile(rf"(?<=\d)[{HYPHENS}](?=\d)")


def excel_serial_to_date(v: str) -> str | None:
    if re.fullmatch(r"\d{5}(\.0+)?", v):
        d = dt.date(1899, 12, 30) + dt.timedelta(days=int(float(v)))
        if 1900 < d.year < 2100:
            return d.isoformat()
    return None


def fraction_to_time(v: str) -> str | None:
    try:
        f = float(v)
    except ValueError:
        return None
    if 0 <= f < 1 and not re.fullmatch(r"\d{1,2}", v):
        minutes = round(f * 24 * 60)
        return f"{minutes // 60:02d}:{minutes % 60:02d}"
    return None


def clean_row(r: dict, prefecture: str, city: str, wards: tuple = ()) -> list[tuple[str, str, str, str]]:
    fixes = []

    def fix(col: str, new: str, why: str) -> None:
        if r[col] != new:
            fixes.append((col, r[col], new, why))
            r[col] = new

    for col in DATE_COLUMNS:
        if r.get(col):
            d = excel_serial_to_date(r[col])
            if d:
                fix(col, d, "Excelの日付シリアル値を日付に変換")
            m = re.fullmatch(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})", nfkc(r[col]))
            if m:
                fix(col, f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", "日付をYYYY-MM-DDの形に統一")
    for col in TIME_COLUMNS:
        if r.get(col):
            t = fraction_to_time(r[col])
            if t:
                fix(col, t, "Excelの時刻（小数）をHH:MMに変換")
            m = re.fullmatch(r"(\d{1,2})[:：](\d{2})", nfkc(r[col]))
            if m:
                fix(col, f"{int(m.group(1)):02d}:{m.group(2)}", "時刻をHH:MMの形に統一")
    v = nfkc(r.get("所在地_市区町村", ""))
    if v and v.startswith(city):
        ward = next((w for w in sorted(wards, key=len, reverse=True) if v[len(city):].startswith(w)), "")
        if v != city + ward:  # 政令指定都市は「さいたま市浦和区」まで
            fix("所在地_市区町村", city + ward, "市区町村の欄に住所全体が入っていた")
    for col in ("所在地_町字", "所在地_番地以下"):
        if r.get(col):
            fix(col, NUMBER_HYPHEN.sub("-", nfkc(r[col])), "全角・半角の表記ゆれを統一")
    if r.get("所在地_連結表記"):
        fix("所在地_連結表記", NUMBER_HYPHEN.sub("-", nfkc(r["所在地_連結表記"])), "全角・半角の表記ゆれを統一")
        rest = r["所在地_連結表記"][len(prefecture + city):] if r["所在地_連結表記"].startswith(prefecture + city) else ""
        if PREFECTURE_RE.match(rest) and not rest.startswith(prefecture + city):
            fix("所在地_連結表記", rest, "市外の住所の前に市の名前が付いていた")
        if re.search(r"地内番$", r["所在地_連結表記"]):  # 「上木崎1丁目地内番」
            fix("所在地_連結表記", r["所在地_連結表記"][:-1], "住所の末尾の不要な「番」を削除")
        for dup in (prefecture + city + city, prefecture + city + prefecture + city):
            if r["所在地_連結表記"].startswith(dup):
                fix("所在地_連結表記", prefecture + city + r["所在地_連結表記"][len(dup):], "住所の市区町村名が重複していた")
    if r.get("法人番号") and not re.fullmatch(r"\d{13}", r["法人番号"]):
        fix("法人番号", "", "法人番号が13桁でない（Excelの指数表記などで桁が失われた）ため空欄に")
    for col in ("電話番号", "連絡先電話番号"):
        if r.get(col):
            v = normalize_phone(r[col])
            v = re.sub(r"^\((\d+)\)[\s-]*", r"\1-", v)
            fix(col, v, "電話番号の表記を統一")
    return fixes


def cell_to_text(v) -> str:
    """Excel のセルの値を文字列に（日付・時刻・整数の小数点を整える）。"""
    if v is None:
        return ""
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if v.time() == dt.time(0) else v.isoformat(sep=" ", timespec="minutes")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, dt.time):
        return v.strftime("%H:%M")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()
