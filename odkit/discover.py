"""埼玉県オープンデータポータルサイトから、市町村のデータセットを探して設定の下書きを作る。

  python -m odkit discover 川口市 --slug kawaguchi

1. 市町村名から組織（organization_id）を探す
2. その市町村のデータセット一覧を全ページたどる
3. 標準データセット（AED・公共施設・文化財・観光・指定緊急避難場所）に当たりそうなものを選ぶ
4. 各リソースのファイル名・最終更新日・ダウンロードURLを調べる
   （川口市のように、ポータルには市のサイトへのリンクだけを載せている市町村もある。その場合はリンク先を使う）
5. work/<slug>/discover.json と cities/<slug>.draft.toml を書き出す

列の対応づけは、この下書きをもとに fetch → inspect → /odkit-map-columns で行う。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

PORTAL = "https://opendata.pref.saitama.lg.jp"

# 標準データセット → データセット名に含まれる語（除外する語）
TARGETS = {
    "aed": (["AED"], []),
    "public_facility": (["公共施設"], ["トイレ"]),
    "cultural_property": (["文化財"], []),
    "tourism": (["観光施設", "観光地", "観光"], []),
    "evacuation_space": (["緊急避難場所"], []),
}


class _Anchors(HTMLParser):
    """<a href> とその中の文字を集める。"""

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.texts: list[str] = []
        self._href: str | None = None
        self._buf: list[str] = []
        self.orgs: list[tuple[str, str]] = []
        self._org: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a":
            self._href = a.get("href") or ""
            self._buf = []
            self._org = a.get("data-id") if a.get("data-type") == "organization" else None

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            # リンク内の最初の文字のかたまり（タイトル）だけを使う。説明文が続くことがあるため
            chunks = [c.strip() for c in self._buf if c.strip()]
            text = chunks[0] if chunks else ""
            self.links.append((self._href, text))
            if self._org:
                self.orgs.append((self._org, text))
            self._href = None

    def handle_data(self, data):
        self.texts.append(data)
        if self._href is not None:
            self._buf.append(data)

    @property
    def text(self) -> str:
        return " ".join(" ".join(self.texts).split())


def parse(html: str) -> _Anchors:
    p = _Anchors()
    p.feed(html)
    return p


def get(path: str, interval: float = 0.5) -> str:
    time.sleep(interval)  # ポータルに負荷をかけない
    req = urllib.request.Request(PORTAL + path, headers={"User-Agent": "municipal-opendata-kit"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode("utf-8", errors="replace")


def find_org(page: _Anchors, city: str) -> tuple[str, int] | None:
    """検索結果の左側の「組織」欄から、市町村名と件数を取り出す（例：越谷市（128））。"""
    for org_id, text in page.orgs:
        m = re.fullmatch(r"(.+?)[（(](\d+)[）)]", text)
        if m and m.group(1) == city:
            return org_id, int(m.group(2))
    return None


def dataset_links(page: _Anchors) -> dict[str, str]:
    out = {}
    for href, text in page.links:
        m = re.search(r"/datasets/(\d+)$", href)
        if m and text:
            out[m.group(1)] = text
    return out


def resource_links(page: _Anchors) -> dict[str, str]:
    out = {}
    for href, text in page.links:
        m = re.search(r"/resources/(\d+)$", href)
        if m:
            out[m.group(1)] = text
    return out


def resource_info(page: _Anchors) -> dict:
    t = page.text
    name = re.search(r"ファイル名\s+(.+?)\s+ダウンロード", t)
    updated = re.search(r"最終更新\s+(\d{4}年\d{1,2}月\d{1,2}日)", t)
    info = {"file": name.group(1).strip() if name else "", "updated": updated.group(1) if updated else ""}
    link = re.search(r"\bURL\s+(https?://\S+)", t)
    if not name and link:  # ファイルではなく、市町村のサイトへのリンクとして登録されているもの
        info.update({"file": link.group(1).rsplit("/", 1)[-1], "url": link.group(1)})
    return info


def classify(title: str) -> str | None:
    for key, (words, excludes) in TARGETS.items():
        if any(w in title for w in words) and not any(x in title for x in excludes):
            return key
    return None


def discover(city: str, slug: str, base: Path) -> dict:
    search = parse(get("/datasets?q=" + urllib.parse.quote(city)))
    org = find_org(search, city)
    if not org:
        raise SystemExit(f"ポータルに「{city}」の組織が見つかりませんでした")
    org_id, count = org
    datasets: dict[str, str] = {}
    for page_no in range(1, 50):
        found = dataset_links(parse(get(f"/datasets?organization_id={org_id}&page={page_no}")))
        new = {k: v for k, v in found.items() if k not in datasets}
        if not new:
            break
        datasets.update(new)

    picks: dict[str, list] = {k: [] for k in TARGETS}
    for ds_id, title in datasets.items():
        key = classify(title)
        if not key:
            continue
        res = []
        for res_id, res_title in resource_links(parse(get(f"/datasets/{ds_id}"))).items():
            info = resource_info(parse(get(f"/resources/{res_id}")))
            res.append({"id": res_id, "title": res_title, **info,
                        "download": info.get("url") or f"{PORTAL}/resource_download/{res_id}"})
        picks[key].append({"dataset_id": ds_id, "title": title, "page": f"{PORTAL}/datasets/{ds_id}", "resources": res})

    result = {"city": city, "organization_id": org_id, "dataset_count": count,
              "all_datasets": datasets, "candidates": picks}
    out = base / "work" / slug / "discover.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    write_draft(result, slug, base / "cities" / f"{slug}.draft.toml")
    return result


def latest(resources: list[dict]) -> dict | None:
    """更新日の新しい CSV / Excel を選ぶ。"""
    files = [r for r in resources if re.search(r"\.(csv|xlsx)\s*$", r["file"], re.I)] or resources
    def key(r):
        m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", r["updated"])
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    return max(files, key=key) if files else None


def write_draft(result: dict, slug: str, path: Path) -> None:
    lines = [
        f"# {result['city']} の設定の下書き（odkit discover が自動生成）",
        "# 1. [city] の code（全国地方公共団体コード）と bbox を確かめて埋める",
        "# 2. python -m odkit fetch → python -m odkit inspect → Claude Code で /odkit-map-columns",
        "# 3. 仕上がったら cities/" + slug + ".toml に名前を変える",
        "",
        'standard_version = "2026-08-01"',
        "",
        "[city]",
        f'slug = "{slug}"',
        'code = ""            # 要確認：全国地方公共団体コード（6桁）',
        'prefecture = "埼玉県"',
        f'name = "{result["city"]}"',
        f'id_prefix = "{slug.upper()}"',
        "bbox = [0, 0, 0, 0]  # 要確認：[南端, 西端, 北端, 東端]",
        'license = "公共データ利用規約 第1.0版（PDL1.0）"',
        "",
        "[reference]",
        'dataset = "public_facility"',
        'title = "公共施設一覧"',
        f'name_strip = ["{result["city"]}立"]',
        "",
    ]
    for key, cands in result["candidates"].items():
        if not cands:
            lines += [f"# [datasets.{key}] … ポータルに見つかりませんでした", ""]
            continue
        cand = cands[0]
        res = latest(cand["resources"])
        if not res:
            continue
        fmt = "xlsx" if res["file"].lower().endswith("xlsx") else "csv"
        lines += [
            f"[datasets.{key}]",
            f'standard = "{key}"',
            f'title = "{cand["title"]}"',
            f'page = "{cand["page"]}"',
            f'source = "{res["download"]}"   # {res["file"]}（'
            + ("市のサイトへのリンク。ポータルの最終更新日は登録時のもの" if res.get("url") else f"最終更新 {res['updated']}") + "）",
            f'format = "{fmt}"',
            f'id_code = "{key[:3].upper()}"',
            "# passthrough = true   # 元データがすでに標準の列名なら",
            f"# [datasets.{key}.columns]  ← inspect の結果を見て /odkit-map-columns で埋める",
            "",
        ]
        if len(cands) > 1:
            lines.insert(-1, "# ほかの候補: " + "、".join(c["title"] for c in cands[1:]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", "utf-8")
