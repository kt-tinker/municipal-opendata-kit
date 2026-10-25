"""出力フォルダの README（出典・加工内容・位置の付け方・食い違い）を作る。"""

from __future__ import annotations

from pathlib import Path

LABELS = {
    "source": "元データの値", "reference": "公共施設情報から転記", "address": "住所検索（番地）",
    "block": "住所検索（街区）", "town": "町字の代表点（要確認）", "none": "付与できず",
}


def write_readme(cfg: dict, summary: dict, out_dir: Path) -> Path:
    city = cfg["city"]
    lines = [
        f"# {city['prefecture']}{city['name']} オープンデータ 自治体標準形式変換版",
        "",
        "このフォルダのデータは municipal-opendata-kit で自動生成しました。",
        "",
        "## ファイル一覧",
        "",
        "| ファイル | 件数 | 元データで位置があった件数 |",
        "| --- | --- | --- |",
    ]
    for key, ds in cfg["datasets"].items():
        s = summary["datasets"][key]
        lines.append(f"| {city['code']}_{ds['standard']}.csv | {s['total']} | {s['had_coords']} |")
    lines += [
        "",
        "- 文字コード：UTF-8（BOM付き）、改行：CRLF",
        f"- 項目：デジタル庁「自治体標準オープンデータセット」（{cfg['standard_version']}版）",
        "",
        f"## 出典（{city.get('license', '各データの利用規約に従う')}）",
        "",
    ]
    for ds in cfg["datasets"].values():
        page = ds.get("page", "")
        lines.append(f"- {ds.get('title', ds['standard'])} {page}".rstrip())
    lines += [
        "- 緯度・経度の一部：国土地理院「住所検索API」",
        "",
        "## 緯度・経度の付け方",
        "",
        "| データ | " + " | ".join(LABELS.values()) + " |",
        "| --- |" + " --- |" * len(LABELS),
    ]
    for key, ds in cfg["datasets"].items():
        p = summary["datasets"][key]["precision"]
        lines.append(f"| {ds.get('title', key)} | " + " | ".join(str(p.get(k, 0)) for k in LABELS) + " |")
    lines += ["", "各行の備考欄にも、位置をどう付けたかを書いています。", ""]
    if summary["mismatches"]:
        lines += ["## 資料間で住所が食い違う施設", "", "座標は公共施設情報の値を採用しています。", ""]
        for m in summary["mismatches"]:
            lines.append(f"- {m['name']}：{m['address']}／公共施設情報「{m['reference_address']}」")
        lines.append("")
    if summary.get("fixes"):
        groups: dict[tuple, list] = {}
        for f in summary["fixes"]:
            groups.setdefault((f["dataset"], f["why"]), []).append(f)
        lines += ["## 元データの表記を直した箇所", "", "| データ | 内容 | 件数 | 例 |", "| --- | --- | --- | --- |"]
        for (ds, why), fs in groups.items():
            ex = fs[0]
            sample = f"「{ex['before']}」→「{ex['after']}」" if ex["after"] else f"「{ex['before']}」→（空欄）"
            lines.append(f"| {cfg['datasets'][ds].get('title', ds)} | {why} | {len(fs)} | {sample} |")
        lines += ["", "1件ずつの記録は work/ の summary.json にあります。", ""]
    lines += [
        "## 注意事項",
        "",
        "- 元データの更新時点によっては、現在の施設の状況と異なる場合があります",
        "- 「町字の代表点（要確認）」の位置は、実際の場所とずれている可能性があります",
    ]
    if summary["review_pending"]:
        lines.append(f"- 未確認の項目が {summary['review_pending']} 件あります（work/{city['slug']}/review/queue.json）")
    path = out_dir / "README.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return path
