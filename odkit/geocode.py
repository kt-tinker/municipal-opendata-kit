"""国土地理院「住所検索API」による位置の付与と、その精度の判定。

結果はキャッシュ（JSON）に保存し、同じ住所を二度問い合わせない。
--offline のときはキャッシュだけを使う（ネットワークに出られない環境やテスト用）。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .normalize import canonical_address, strip_city

GSI_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch?q="

# 備考欄に書く文言。アプリ側はこの文言で位置の精度を判定する（「同名施設」「住所検索」「街区レベル」「町字の代表点」）
NOTE_ADDRESS = "緯度経度は住所から国土地理院の住所検索で付与"
NOTE_BLOCK = "緯度経度は住所から国土地理院の住所検索で付与（街区レベル）"
NOTE_TOWN = "緯度経度は町字の代表点のため位置は要確認"
NOTE_DMS = "緯度経度は元データの度分秒を10進数に変換"


def note_reference(city: str, ref_title: str) -> str:
    return f"緯度経度は{city}「{ref_title}」の同名施設の値を転記"


class Geocoder:
    def __init__(self, cache_path: Path, offline: bool = False, interval: float = 1.0):
        self.cache_path = cache_path
        self.offline = offline
        self.interval = interval
        self.cache: dict = json.loads(cache_path.read_text("utf-8")) if cache_path.exists() else {}
        self._last = 0.0
        self.live_calls = 0

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1), "utf-8")

    def search(self, address: str) -> list[dict] | None:
        """[{lat, lon, title}] を返す。オフラインでキャッシュになければ None。"""
        if address in self.cache:
            return self.cache[address]
        if self.offline:
            return None
        wait = self.interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            GSI_URL + urllib.parse.quote(address),
            headers={"User-Agent": "municipal-opendata-kit"},
        )
        with urllib.request.urlopen(req, timeout=20) as res:
            data = json.load(res)
        self._last = time.time()
        self.live_calls += 1
        results = [
            {"lon": f["geometry"]["coordinates"][0], "lat": f["geometry"]["coordinates"][1], "title": f["properties"]["title"]}
            for f in data
        ]
        self.cache[address] = results
        return results


def judge_precision(query: str, title: str, prefecture: str, city: str) -> str:
    """問い合わせた住所と、返ってきた地名を比べて精度を決める。

    address = 番地まで一致 / block = 街区（最初の番号）まで / town = 町字（丁目）まで / none = 市外・不一致
    """
    q = strip_city(canonical_address(query), prefecture, city)
    t = canonical_address(title)
    if not t.startswith(prefecture + city) and not t.startswith(city):
        return "none"
    t = strip_city(t, prefecture, city)
    q, t = q.replace("大字", ""), t.replace("大字", "")  # 「大字」の有無は区別しない
    town_q = re.match(r"^[^\d]+(?:\d+丁目)?", q)
    town_t = re.match(r"^[^\d]+(?:\d+丁目)?", t)
    if not town_q or not town_t or town_q.group(0) != town_t.group(0):
        return "none"
    q_nums = re.findall(r"\d+", q[len(town_q.group(0)):])
    t_nums = re.findall(r"\d+", t[len(town_t.group(0)):])
    if q_nums and t_nums == q_nums[: len(t_nums)]:
        if len(t_nums) >= min(2, len(q_nums)):
            return "address"
        if len(t_nums) >= 1:
            return "block"
    return "town"


def best_match(results: list[dict], query: str, prefecture: str, city: str):
    """最も精度の高い候補と、その精度を返す。"""
    order = {"address": 0, "block": 1, "town": 2, "none": 3}
    best, best_p = None, "none"
    for r in results or []:
        p = judge_precision(query, r["title"], prefecture, city)
        if best is None or order[p] < order[best_p]:
            best, best_p = r, p
    return best, best_p
