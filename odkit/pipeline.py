"""設定ファイル（cities/<市>.toml）に従って、元データを標準形式に整える本体。

流れ：読み込み → 項目の対応づけ → 値の整形 → 住所の分割 → 位置の付与 → 検証 → 出力
判断が要るところは review/queue.json に出し、review/decisions.json の判断を次の実行で反映する。
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import geocode as geo
from .normalize import (
    canonical_address,
    full_address,
    join_split,
    nfkc,
    normalize_phone,
    outside_city,
    split_address,
    split_count,
    strip_city,
    to_decimal_degree,
    wareki_to_iso,
)
from .clean import clean_row
from .review import Review
from .tables import read_table, write_std_csv
from .townmaster import TownMaster, compare_key

KIT_ROOT = Path(__file__).resolve().parent.parent
PRECISIONS = ("source", "reference", "address", "block", "town", "none")

# 以前の版の標準で使われていた列名 → 今の版の列名（passthrough のときに読み替える）
OLD_COLUMN_NAMES = {
    "建物名等(方書)": "建物名等（方書）",
    "バリアフリートイレ": "多目的トイレ",
    "盲導犬・介助犬、聴導犬同伴": "盲導犬・介助犬・聴導犬同伴",
    "画像_ライセンス": "画像ライセンス",
}


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_path"] = path.resolve()
    cfg["_base"] = path.resolve().parent.parent  # cities/ の一つ上をリポジトリの基点とみなす
    return cfg


def standard_columns(version: str, name: str) -> list[str]:
    return json.loads((KIT_ROOT / "standards" / version / f"{name}.json").read_text("utf-8"))["columns"]


def building_column(cols: list[str]) -> str:
    return next(c for c in cols if c.startswith("建物名等"))


def render(spec: str, src: dict) -> str:
    """"列名" ならその値。"見出し：{列A}／見出し：{列B}" なら差し込み。

    空の列は、その直前の文字列ごと省く（「利用可能日：」だけが残らないように）。
    """
    if "{" not in spec:
        return src.get(spec, "").strip()
    pairs = re.findall(r"([^{]*)\{([^}]+)\}", spec)
    tail = spec[spec.rfind("}") + 1:]
    out = "".join(pre + src.get(col, "").strip() for pre, col in pairs if src.get(col, "").strip())
    if out and pairs and src.get(pairs[-1][1], "").strip():
        out += tail
    return out.strip(" ／/、")


def valid_coord(lat, lon, bbox) -> bool:
    if lat is None or lon is None:
        return False
    s, w, n, e = bbox
    return s <= lat <= n and w <= lon <= e


@dataclass
class Stats:
    total: int = 0
    had_coords: int = 0
    precision: dict = field(default_factory=lambda: {p: 0 for p in PRECISIONS})


class Pipeline:
    def __init__(self, cfg: dict, offline: bool = False):
        self.cfg = cfg
        self.city = cfg["city"]
        self.base = cfg["_base"]
        self.work = self.base / "work" / self.city["slug"]
        self.out = self.base / "output" / self.city["slug"]
        self.review = Review(self.work / "review")
        self.geocoder = geo.Geocoder(self.work / "geocode_cache.json", offline=offline)
        self.stats: dict[str, Stats] = {}
        self.mismatches: list[dict] = []
        self.fixes: list[dict] = []
        self.reference_index: dict[str, dict] = {}
        self.reference_cfg = cfg.get("reference", {})
        self.outside: list[dict] = []
        self.town_master = None
        if self.town_master_path.exists():
            self.town_master = TownMaster.load(self.town_master_path)

    @property
    def town_master_path(self) -> Path:
        """アドレス・ベース・レジストリの町字マスター（fetch で取得）。"""
        custom = self.city.get("town_master")
        if isinstance(custom, str):
            return self.base / custom
        return self.work / "abr" / f"mt_town_city{self.city['code']}.csv.zip"

    # ---------- 共通 ----------
    @property
    def pref(self) -> str:
        return self.city["prefecture"]

    @property
    def name(self) -> str:
        return self.city["name"]

    def ref_key(self, name: str) -> str:
        n = nfkc(name).replace("ヶ", "ケ").replace("ヵ", "カ")  # 「越ヶ谷」「越ケ谷」の揺れ
        for s in self.reference_cfg.get("name_strip", []):
            n = n.replace(nfkc(s).replace("ヶ", "ケ"), "")
        return re.sub(r"\s", "", n)

    def source_path(self, inp: dict, key: str) -> Path:
        src = inp["source"]
        if src.startswith("http"):
            return self.work / "raw" / key / (Path(src).name or "source")
        return self.base / src

    @staticmethod
    def inputs(ds: dict) -> list[dict]:
        """1つのデータセットが複数の元データからなる場合（例：観光地情報＋景観情報）は inputs に並べる。"""
        return ds.get("inputs") or [ds]

    # ---------- 1件分 ----------
    def build_row(self, key: str, ds: dict, inp: dict, cols: list[str], src: dict, index: int) -> tuple[dict, str]:
        """inp = 元データ1つ分の設定（列の対応・備考に回す列など）。ds = データセット全体の設定。"""
        r = {c: "" for c in cols}
        notes: list[str] = []
        bcol = building_column(cols)

        if inp.get("passthrough"):  # 元データがすでに標準の列名を使っている場合
            for c in cols:
                if c in src:
                    r[c] = src[c].strip()
            for old, new in OLD_COLUMN_NAMES.items():
                if new in r and new not in src and src.get(old, "").strip():
                    r[new] = src[old].strip()
        for std_col, spec in inp.get("columns", {}).items():
            if std_col not in r:
                raise SystemExit(f"[{key}] 標準にない項目名です: {std_col}")
            r[std_col] = render(spec, src)
        code = self.city["code"]
        defaults = {
            "全国地方公共団体コード": code, "地方公共団体名": self.pref + self.name,
            "所在地_全国地方公共団体コード": code, "所在地_都道府県": self.pref, "所在地_市区町村": self.name,
        }
        for k, v in {**defaults, **inp.get("constants", {})}.items():
            if k in r and not r[k]:
                r[k] = v
        def fix(col: str, after: str, why: str) -> None:
            if r[col] != after:
                self.fixes.append({"dataset": key, "id": r["ID"] or f"#{index + 1}", "column": col,
                                   "before": r[col], "after": after, "why": why})
                r[col] = after

        for col, before, after, why in clean_row(r, self.pref, self.name):
            self.fixes.append({"dataset": key, "id": r["ID"] or f"#{index + 1}", "column": col,
                               "before": before, "after": after, "why": why})
        if not r["ID"]:
            r["ID"] = f"{self.city['id_prefix']}-{ds['id_code']}-{index + 1:0{ds.get('id_digits', 3)}d}"
        for spec in inp.get("notes", []):
            col, label = (spec["column"], spec.get("label", spec["column"])) if isinstance(spec, dict) else (spec, spec)
            v = src.get(col, "").strip()
            if v:
                notes.append(f"{label}：{v}")
        if inp.get("note"):
            notes.append(inp["note"])
        cs = inp.get("count_split")
        if cs and src.get(cs["column"]):
            r[cs["number"]], r[cs["unit"]] = split_count(src[cs["column"]])

        # 和暦の日付 → 西暦（元の表記は備考に残す）
        for col in inp.get("wareki", []):
            if r.get(col):
                iso = wareki_to_iso(r[col])
                if iso:
                    notes.insert(0, f"{col}（元表記）：{r[col]}")
                    r[col] = iso
        for col in ("電話番号", "連絡先電話番号"):
            if r.get(col):
                r[col] = normalize_phone(r[col])

        item_base = f"{key}:{r['ID']}"
        self.fill_address(r, bcol, item_base, fix)
        precision = self.fill_coords(key, ds, r, notes, item_base)
        r.pop("_outside", None)
        r["備考"] = "／".join([n for n in [r.get("備考", "")] + notes if n])
        return r, precision

    def fill_address(self, r: dict, bcol: str, item_base: str, fix) -> None:
        if not r["所在地_連結表記"]:
            return
        if outside_city(r["所在地_連結表記"], self.pref, self.name):
            r["_outside"] = True  # 市外の施設：住所はそのまま
            return
        r["所在地_連結表記"] = full_address(r["所在地_連結表記"], self.pref, self.name)
        tm = self.town_master
        if r["所在地_町字"]:
            if tm and "町字ID" in r and not r["町字ID"]:
                hit = tm.town_id(r["所在地_町字"])
                if hit:
                    if hit[0] != nfkc(r["所在地_町字"]):
                        fix("所在地_町字", hit[0], "町字の表記を町字マスターに合わせた（丁目の補完など）")
                    r["町字ID"] = hit[1]
            return
        rest = strip_city(canonical_address(r["所在地_連結表記"]), self.pref, self.name)
        split = tm.split(r["所在地_連結表記"], self.pref, self.name) if tm else split_address(r["所在地_連結表記"], self.pref, self.name)
        item_id = f"{item_base}:address_split"
        if split is None:
            context = {"address": r["所在地_連結表記"], "rest": rest}
            if tm:
                context["town_master"] = "町字は町字マスターにあるもの（output の README 参照）から選ぶ"
            d = self.review.decision(item_id, "address_split")
            if d:
                cand = d.get("split", {})
                error = None
                if compare_key(join_split(cand)) != compare_key(rest):
                    error = "分けた3つをつなげても元の住所に戻りません"
                elif tm and not tm.town_id(cand.get("所在地_町字", "")):
                    error = "町字が町字マスターにありません"
                if error:
                    self.review.ask(item_id, "address_split", context, error=error)
                    return
                split = dict(cand)
                if tm:
                    split["所在地_町字"], split["町字ID"] = tm.town_id(cand["所在地_町字"])
                self.review.record(item_id, d)
            else:
                self.review.ask(item_id, "address_split", context)
                return
        r["所在地_町字"] = split["所在地_町字"]
        r["所在地_番地以下"] = split["所在地_番地以下"]
        r[bcol] = split.get("建物名等", "")
        if split.get("町字ID") and "町字ID" in r and not r["町字ID"]:
            r["町字ID"] = split["町字ID"]

    def fill_coords(self, key: str, ds: dict, r: dict, notes: list[str], item_base: str) -> str:
        bbox = self.city["bbox"]
        # 1つのセルに複数の座標（「35.90、35.92」）→ 1つ目を採り、残りは備考に残す
        multi = [re.split(r"\s*[、,，]\s*", nfkc(r[c])) for c in ("緯度", "経度")]
        if len(multi[0]) > 1 and len(multi[0]) == len(multi[1]):
            extra = "、".join(f"({a}, {b})" for a, b in zip(multi[0][1:], multi[1][1:]))
            notes.append(f"元データに複数の地点があったため1つ目を採用（ほかの地点：{extra}）")
            r["緯度"], r["経度"] = multi[0][0], multi[1][0]
        lat, lon = to_decimal_degree(r["緯度"]), to_decimal_degree(r["経度"])
        if r.get("_outside"):
            # 市外の施設は市域の範囲チェックができない。元データの座標をそのまま使い、なければ付けない
            self.outside.append({"dataset": key, "id": r["ID"], "name": r["名称"], "address": r["所在地_連結表記"],
                                 "has_coords": lat is not None and lon is not None})
            if lat is None or lon is None:
                r["緯度"] = r["経度"] = ""
                notes.append("市外にある施設のため緯度経度は付与していない")
                return "none"
            r["緯度"], r["経度"] = str(lat), str(lon)
            notes.append("市外にある施設のため、緯度経度は市域の範囲チェックをせず元データの値を使用")
            self.stats[key].had_coords += 1
            return "source"
        if valid_coord(lat, lon, bbox):
            if nfkc(r["緯度"]) != str(lat) and not re.fullmatch(r"-?\d+(\.\d+)?", nfkc(r["緯度"])):
                notes.append(geo.NOTE_DMS)
            r["緯度"], r["経度"] = str(lat), str(lon)
            self.stats[key].had_coords += 1
            return "source"
        r["緯度"] = r["経度"] = ""

        # 1) 公共施設情報の同名施設から転記
        if key != self.reference_cfg.get("dataset") and self.reference_index:
            hit = self.reference_index.get(self.ref_key(r["名称"]))
            if hit is None:
                hit = self.fuzzy_reference(key, r, item_base)
            if hit:
                self.check_mismatch(key, r, hit, item_base)
                r["緯度"], r["経度"] = hit["緯度"], hit["経度"]
                notes.append(geo.note_reference(self.name, self.reference_cfg.get("title", "公共施設情報")) + f"（{hit['ID']}{self.ref_origin(hit)}）")
                return "reference"

        # 2) 住所から国土地理院で付与（AI の判断で住所を直して再検索することもある）
        item_id = f"{item_base}:geocode"
        d = self.review.decision(item_id, "geocode")
        query = r["所在地_連結表記"]
        if self.town_master and r.get("町字ID") and r["所在地_町字"]:
            # 町字マスターで丁目を確かめた住所で検索する（「青木2-1-1」→「青木2丁目1-1」）
            num = r["所在地_番地以下"] if re.match(r"\d", r["所在地_番地以下"]) else ""
            query = self.pref + self.name + r["所在地_町字"] + num
        if d and d["action"] == "use_reference":
            hit = self.reference_index.get(self.ref_key(d.get("name", "")))
            if hit:
                self.review.record(item_id, d)
                r["緯度"], r["経度"] = hit["緯度"], hit["経度"]
                notes.append(geo.note_reference(self.name, self.reference_cfg.get("title", "公共施設情報")) + f"（{hit['ID']}・判断による{self.ref_origin(hit)}）")
                return "reference"
            d = None
        if d and d["action"] == "no_location":  # 無形文化財・天然記念物の動物など、場所を持たないもの
            self.review.record(item_id, d)
            notes.append("所在地を持たないため緯度経度なし（判断による）")
            return "none"
        if d and d["action"] in ("retry_address", "accept_town") and d.get("address"):
            alt = full_address(d.get("address", ""), self.pref, self.name)
            if not alt.startswith(self.pref + self.name):
                d = None
            else:
                query = alt
        if not query:
            self.review.ask(item_id, "geocode", {"name": r["名称"], "address": "", "note": "住所がありません"})
            return "none"
        results = self.geocoder.search(query)
        if results is None:
            note = "オフラインのため未検索" if self.geocoder.offline else "住所検索に失敗したため未検索（次の build で再検索）"
            self.review.ask(item_id, "geocode", {"name": r["名称"], "address": r["所在地_連結表記"], "searched": query, "note": note})
            return "none"
        best, p = geo.best_match(results, query, self.pref, self.name)
        if best and p in ("address", "block") and valid_coord(best["lat"], best["lon"], bbox):
            r["緯度"], r["経度"] = str(best["lat"]), str(best["lon"])
            notes.append(geo.NOTE_ADDRESS if p == "address" else geo.NOTE_BLOCK)
            if d:
                self.review.record(item_id, d)
                notes.append(f"住所を「{query}」として検索（判断による）")
            return p
        if best and p == "town" and valid_coord(best["lat"], best["lon"], bbox) and d and d["action"] == "accept_town":
            self.review.record(item_id, d)
            r["緯度"], r["経度"] = str(best["lat"]), str(best["lon"])
            notes.append(geo.NOTE_TOWN)
            if d.get("address"):
                notes.append(f"住所がないため「{query}」として検索（判断による）")
            return "town"
        candidates = [{"title": x["title"], "precision": geo.judge_precision(query, x["title"], self.pref, self.name)} for x in (results or [])[:5]]
        self.review.ask(item_id, "geocode", {
            "name": r["名称"], "address": r["所在地_連結表記"], "searched": query, "candidates": candidates,
            "reference_names": self.similar_reference_names(r["名称"]),
        }, error=("修正後の住所でも番地・街区まで特定できませんでした" if d else None))
        return "none"

    @staticmethod
    def ref_origin(ref: dict) -> str:
        """転記元の公共施設の位置が元データの値でない場合（住所検索で付けた位置）はそれを書き添える。"""
        if "街区レベル" in ref.get("備考", ""):
            return "・転記元の位置は住所検索（街区レベル）"
        if geo.NOTE_ADDRESS in ref.get("備考", ""):
            return "・転記元の位置は住所検索"
        return ""

    def similar_reference_names(self, name: str, limit: int = 5) -> list[str]:
        key = self.ref_key(name)
        if len(key) < 2:
            return []
        scored = []
        for k, ref in self.reference_index.items():
            common = len(set(key) & set(k))
            if common >= max(2, len(key) // 2):
                scored.append((common, ref["名称"]))
        return [n for _, n in sorted(scored, reverse=True)[:limit]]

    def fuzzy_reference(self, key: str, r: dict, item_base: str):
        """名前の一部が一致する施設。確信がないので AI／人の判断を待つ。"""
        k = self.ref_key(r["名称"])
        cands = [ref for rk, ref in self.reference_index.items() if len(k) >= 3 and (k in rk or rk in k) and len(rk) >= 3]
        if not cands:
            return None
        item_id = f"{item_base}:name_match"
        d = self.review.decision(item_id, "name_match")
        if d:
            if d["action"] == "use_reference":
                hit = next((c for c in cands if c["名称"] == d.get("name")), None)
                if hit:
                    self.review.record(item_id, d)
                    return hit
            else:
                self.review.record(item_id, d)
                return None
        self.review.ask(item_id, "name_match", {
            "name": r["名称"], "address": r["所在地_連結表記"],
            "candidates": [{"name": c["名称"], "address": c["所在地_連結表記"], "id": c["ID"]} for c in cands[:5]],
        })
        return None

    def check_mismatch(self, key: str, r: dict, ref: dict, item_base: str) -> None:
        a = strip_city(canonical_address(r["所在地_連結表記"]), self.pref, self.name)
        b = strip_city(canonical_address(ref["所在地_連結表記"]), self.pref, self.name)
        if not a or not b or a == b:
            return
        entry = {"dataset": key, "id": r["ID"], "name": r["名称"], "address": r["所在地_連結表記"],
                 "reference_id": ref["ID"], "reference_address": ref["所在地_連結表記"]}
        item_id = f"{item_base}:mismatch"
        d = self.review.decision(item_id, "mismatch")
        if d:
            self.review.record(item_id, d)
            entry["decision"] = d
        else:
            self.review.ask(item_id, "mismatch", entry)
        self.mismatches.append(entry)

    # ---------- 全体 ----------
    def load_input(self, key: str, inp: dict) -> list[dict]:
        path = self.source_path(inp, key)
        if not path.exists():
            raise SystemExit(f"[{key}] 元データがありません: {path}（fetch を先に実行するか、手で置いてください）")
        _, rows = read_table(path, inp.get("format", "csv"), inp.get("encoding", "auto"), inp.get("sheet"))
        flt = inp.get("filter")
        if flt:
            rows = [s for s in rows if flt["contains"] in s.get(flt["column"], "")]
        return rows

    def run_dataset(self, key: str, ds: dict) -> list[dict]:
        cols = standard_columns(self.cfg["standard_version"], ds["standard"])
        self.stats[key] = Stats()
        rows, seen, i = [], set(), 0
        for inp in self.inputs(ds):
            for n, s in enumerate(self.load_input(key, inp)):
                name_col = inp.get("columns", {}).get("名称", "名称")
                if not render(name_col, s):
                    filled = "、".join(v for v in s.values() if v.strip())[:40]
                    self.fixes.append({"dataset": key, "id": f"元データ{n + 2}行目", "column": "（行）",
                                       "before": filled, "after": "", "why": "名称のない行を除外"})
                    continue
                dedupe = ds.get("dedupe_by_name") and render(name_col, s)
                if dedupe and dedupe in seen:
                    continue  # 複数の元データに同じ施設がある場合は先のものを採る
                seen.add(dedupe)
                row, p = self.build_row(key, ds, inp, cols, s, i)
                i += 1
                self.stats[key].total += 1
                self.stats[key].precision[p] += 1
                rows.append(row)
        write_std_csv(self.out / f"{self.city['code']}_{ds['standard']}.csv", cols, rows)
        return rows

    def run(self) -> dict:
        datasets = self.cfg["datasets"]
        order = list(datasets)
        ref = self.reference_cfg.get("dataset")
        if ref in order:  # 転記元を先に作る
            order.remove(ref)
            order.insert(0, ref)
        try:
            for key in order:
                rows = self.run_dataset(key, datasets[key])
                if key == ref:
                    self.reference_index = {self.ref_key(x["名称"]): x for x in rows if x["緯度"]}
        finally:
            self.geocoder.save()  # Ctrl+C やエラーで止まっても、そこまでの検索結果は残す
        self.review.save()
        summary = {
            "city": self.city["name"],
            "datasets": {k: {"total": s.total, "had_coords": s.had_coords, "precision": s.precision} for k, s in self.stats.items()},
            "review_pending": len(self.review.queue),
            "review_applied": len(self.review.applied),
            "mismatches": self.mismatches,
            "fixes": self.fixes,
            "outside": self.outside,
            "town_master": bool(self.town_master),
            "geocoder_live_calls": self.geocoder.live_calls,
            "geocoder_failed": self.geocoder.failed,
        }
        (self.work / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf-8")
        return summary
