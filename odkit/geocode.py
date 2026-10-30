"""国土地理院「住所検索API」による位置の付与と、その精度の判定。

結果はキャッシュ（JSON）に保存し、同じ住所を二度問い合わせない。
--offline のときはキャッシュだけを使う（ネットワークに出られない環境やテスト用）。

公共施設が1,000件を超える市（川口市）では検索に20分ほどかかり、途中で応答が止まることがある。
- 失敗したら間をあけて再試行し、それでもだめならその住所は「未検索」として先に進む
- 続けて失敗したら、残りは検索せずに進む（次の build で続きから検索する）
- キャッシュはこまめに保存する（途中で止めても、そこまでの結果は残る）
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
    RETRY_WAITS = (5, 20)   # 失敗したときに待つ秒数（再試行2回）
    GIVE_UP_AFTER = 3       # 続けてこの件数失敗したら、残りは検索しない
    SAVE_EVERY = 20         # この件数ごとにキャッシュを保存する

    def __init__(self, cache_path: Path, offline: bool = False, interval: float = 1.0, timeout: float = 20,
                 log=print):
        self.cache_path = cache_path
        self.offline = offline
        self.interval = interval
        self.timeout = timeout
        self.log = log
        self.cache: dict = json.loads(cache_path.read_text("utf-8")) if cache_path.exists() else {}
        self._last = 0.0
        self.live_calls = 0
        self.failed = 0          # 検索できなかった住所の数
        self._fail_streak = 0
        self.gave_up = False     # 続けて失敗したため検索をやめた

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1), "utf-8")

    def search(self, address: str) -> list[dict] | None:
        """[{lat, lon, title}] を返す。オフライン・検索できなかったときでキャッシュになければ None。"""
        if address in self.cache:
            return self.cache[address]
        if self.offline or self.gave_up:
            return None
        data = None
        for attempt, retry_wait in enumerate((0, *self.RETRY_WAITS)):
            if retry_wait:
                self.log(f"  住所検索の応答がありません。{retry_wait}秒待って再試行します（{attempt}/{len(self.RETRY_WAITS)}）")
                time.sleep(retry_wait)
            wait = self.interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            req = urllib.request.Request(
                GSI_URL + urllib.parse.quote(address),
                headers={"User-Agent": "municipal-opendata-kit"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as res:
                    data = json.load(res)
                break
            except (OSError, ValueError) as e:  # タイムアウト・接続エラー・HTTPエラー・壊れた応答
                last_error = e
            finally:
                self._last = time.time()
        if data is None:
            self.failed += 1
            self._fail_streak += 1
            self.log(f"  住所検索に失敗しました：{address}（{type(last_error).__name__}）→ 未検索として先に進みます")
            if self._fail_streak >= self.GIVE_UP_AFTER:
                self.gave_up = True
                self.log(f"  {self.GIVE_UP_AFTER}件続けて失敗したため、残りの住所検索はやめて先に進みます。"
                         "時間をおいて build し直すと、続きから検索します")
            self.save()
            return None
        self._fail_streak = 0
        self.live_calls += 1
        if self.live_calls % self.SAVE_EVERY == 0:
            self.save()
        if self.live_calls % 100 == 0:
            self.log(f"  住所検索 {self.live_calls}件 完了（キャッシュ {len(self.cache)}件）")
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
