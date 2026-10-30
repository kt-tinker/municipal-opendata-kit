"""アドレス・ベース・レジストリ（デジタル庁）の「町字マスター」を使って、住所を町字と番地以下に分け、町字IDを付ける。

元データの住所は「青木2-1-1」のように丁目を省いて書かれることが多い。
「青木」に丁目があるかどうかは住所の形だけでは決まらない（「神戸767-1」は丁目のない町の番地）。
町字マスターには市内の町字（大字・町と丁目）の一覧があるので、これと照らして決める。

- 町字マスターにある町字だけを使う。見つからなければ None を返し、確認リスト（AI／人）に回す
- 「大字」は付けても省いてもよい（「赤山1143」→ 町字「大字赤山」）
- 似た町名（「芝」「芝下」「芝園町」）は長いほうから照らす

データ：https://dataset.address-br.digital.go.jp/ （市区町村ごとの mt_town_city<コード>.csv.zip）
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from pathlib import Path

from .normalize import canonical_address, nfkc, strip_city

URL = "https://data.address-br.digital.go.jp/mt_town/city/mt_town_city{code}.csv.zip"
PAGE = "https://dataset.address-br.digital.go.jp/"
NO_NUMBER = ("地内", "付近", "地先")


def _key(s: str) -> str:
    return nfkc(s).replace("ヶ", "ケ").replace("ヵ", "カ").replace(" ", "")


def compare_key(s: str) -> str:
    """分け方が変わっても同じ住所なら同じになる比較用の表記（「大字」の有無、「2丁目1」と「2-1」を同一視）。"""
    t = _key(canonical_address(s)).replace("大字", "")
    t = re.sub(r"(\d+)丁目-?", r"\1-", t)
    return t.rstrip("-")


class TownMaster:
    def __init__(self, rows: list[dict]):
        self.oaza: dict[str, dict] = {}  # 照合用の名前 → {"name", "id"(丁目なしの町のID), "chome": {番号: (町字, ID)}}
        self.names: dict[str, str] = {}  # 町字（正式な表記）の照合用キー → 町字ID
        for r in rows:
            if r.get("status_flg") == "0" or r.get("ablt_date"):  # 廃止されたもの
                continue
            oaza = nfkc(r["oaza_cho"])
            entry = self.oaza.setdefault(_key(oaza), {"name": oaza, "id": "", "chome": {}})
            if r.get("chome_number"):
                town = f"{oaza}{int(r['chome_number'])}丁目"
                entry["chome"][int(r["chome_number"])] = (town, r["machiaza_id"])
                self.names[_key(town)] = r["machiaza_id"]
            elif not r.get("koaza"):
                entry["id"] = r["machiaza_id"]
                self.names[_key(oaza)] = r["machiaza_id"]
        # 「大字」を省いた書き方でも照らせるようにする（正式な表記を優先）
        self.alias = {k: k for k in self.oaza}
        for k in list(self.oaza):
            if k.startswith("大字") and k[2:] not in self.alias:
                self.alias[k[2:]] = k
        self.lookup_order = sorted(self.alias, key=len, reverse=True)

    @classmethod
    def load(cls, path: Path) -> "TownMaster":
        data = path.read_bytes()
        if path.suffix == ".zip":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                data = z.read(next(n for n in z.namelist() if n.endswith(".csv")))
        return cls(list(csv.DictReader(io.StringIO(data.decode("utf-8-sig")))))

    def town_id(self, town: str) -> tuple[str, str] | None:
        """町字の表記から (正式な町字, 町字ID)。「大沢3」のように丁目を省いたものも照らす。"""
        k = _key(town)
        if k in self.names:
            return self.official(k), self.names[k]
        m = re.fullmatch(r"(.+?)(\d+)", k)
        if m:
            entry = self.oaza.get(self.alias.get(m.group(1), ""))
            if entry and int(m.group(2)) in entry["chome"]:
                return entry["chome"][int(m.group(2))]
        entry = self.oaza.get(self.alias.get(k, ""))
        if entry and entry["id"]:
            return entry["name"], entry["id"]
        return None

    def official(self, key: str) -> str:
        for entry in self.oaza.values():
            if _key(entry["name"]) == key:
                return entry["name"]
            for town, _ in entry["chome"].values():
                if _key(town) == key:
                    return town
        return key

    def split(self, address: str, prefecture: str, city: str) -> dict | None:
        """住所 → {所在地_町字, 所在地_番地以下, 建物名等, 町字ID}。決められなければ None。"""
        rest = _key(strip_city(canonical_address(address), prefecture, city))
        alias = next((a for a in self.lookup_order if rest.startswith(a)), None)
        if alias is None:
            return None
        entry = self.oaza[self.alias[alias]]
        after = rest[len(alias):]
        # 同じ名前の「大字〇〇」（丁目なし）と「〇〇」（丁目あり）がある町（川口市の赤井・芝・戸塚など）：
        # 書かれた番号が「〇〇」の丁目にないなら「大字〇〇」の番地とみなす
        oaza_variant = self.oaza.get("大字" + alias) if not alias.startswith("大字") else None
        if oaza_variant and entry["chome"] and not oaza_variant["chome"]:
            m = re.match(r"(\d+)", after)
            if m and int(m.group(1)) not in entry["chome"] and not re.match(r"\d+丁目", after):
                entry = oaza_variant
        town, town_id = entry["name"], entry["id"]
        if entry["chome"] and entry["id"] and not re.match(r"\d+丁目", after):
            return None  # 丁目のある区域とない区域が混在する町：「12-3」が丁目か番地か決められない
        if entry["chome"]:
            m = re.match(r"(\d+)(?:丁目-?|-|(?=\D)|$)", after)
            if m:
                n = int(m.group(1))
                if n not in entry["chome"]:
                    return None  # 存在しない丁目
                town, town_id = entry["chome"][n]
                after = after[m.end():]
            elif after and after not in NO_NUMBER:
                return None
        elif re.match(r"\d+丁目", after):
            return None  # 丁目のない町に丁目が書かれている
        after = after.lstrip("-")
        if after in NO_NUMBER or not after:
            return {"所在地_町字": town, "所在地_番地以下": after, "建物名等": "", "町字ID": town_id}
        m = re.fullmatch(r"(\d+(?:-\d+)*(?:[、,]\d+(?:-\d+)*)*)(.*)", after)  # 「70-1、74-9」「14,15」もそのまま番地以下に
        if not m:
            return None
        num, bldg = m.group(1), m.group(2).strip()
        if bldg in ("番", "他", "外", "号") or bldg in NO_NUMBER:  # 「6-9地内」「238 地先」
            num, bldg = num + bldg, ""
        elif bldg and re.match(r"[-\d、,]", bldg):
            return None
        return {"所在地_町字": town, "所在地_番地以下": num, "建物名等": bldg, "町字ID": town_id}
