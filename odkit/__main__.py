"""使い方:

  python -m odkit fetch   cities/soka.toml          元データをダウンロード（source が URL のもの）
  python -m odkit inspect cities/soka.toml          元データの列名と先頭行を書き出す（AI の対応づけ用）
  python -m odkit build   cities/soka.toml [--offline]  標準形式に整えて output/ に書き出す
  python -m odkit status  cities/soka.toml          確認待ちの件数を表示
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from .pipeline import Pipeline, load_config, standard_columns
from .report import write_readme
from .tables import read_table


def cmd_fetch(cfg: dict) -> None:
    p = Pipeline(cfg)
    for key, ds in cfg["datasets"].items():
        for inp in p.inputs(ds):
            if not inp["source"].startswith("http"):
                print(f"[{key}] ローカルのファイルを使います: {inp['source']}")
                continue
            dest = p.source_path(inp, key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(inp["source"], headers={"User-Agent": "municipal-opendata-kit"})
            with urllib.request.urlopen(req, timeout=60) as res:
                dest.write_bytes(res.read())
            print(f"[{key}] 取得しました: {dest}")


def cmd_inspect(cfg: dict) -> None:
    p = Pipeline(cfg)
    report = {"city": cfg["city"], "standard_version": cfg["standard_version"], "datasets": {}}
    for key, ds in cfg["datasets"].items():
        entries = []
        for n, inp in enumerate(p.inputs(ds)):
            path = p.source_path(inp, key)
            entry = {"input": n, "source": str(path), "current_mapping": inp.get("columns", {}),
                     "notes": inp.get("notes", []), "passthrough": inp.get("passthrough", False)}
            if path.exists():
                cols, rows = read_table(path, inp.get("format", "csv"), inp.get("encoding", "auto"), inp.get("sheet"))
                flt = inp.get("filter")
                if flt:
                    rows = [r for r in rows if flt["contains"] in r.get(flt["column"], "")]
                used = json.dumps([inp.get("columns", {}), inp.get("notes", []), inp.get("count_split", {})], ensure_ascii=False)
                entry.update({"source_columns": cols, "rows": len(rows), "samples": rows[:5],
                              "unmapped_source_columns": [c for c in cols if f'"{c}"' not in used and "{" + c + "}" not in used]})
            else:
                entry["error"] = "元データがまだありません（fetch するか手で置く）"
            entries.append(entry)
        report["datasets"][key] = {"standard": ds["standard"],
                                   "standard_columns": standard_columns(cfg["standard_version"], ds["standard"]),
                                   "inputs": entries}
    out = p.work / "inspect.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    print(f"書き出しました: {out}")


def cmd_build(cfg: dict, offline: bool) -> int:
    p = Pipeline(cfg, offline=offline)
    summary = p.run()
    readme = write_readme(cfg, summary, p.out)
    print(f"出力: {p.out}")
    for key, s in summary["datasets"].items():
        prec = " ".join(f"{k}={v}" for k, v in s["precision"].items() if v)
        print(f"  {key}: {s['total']}件（元データで位置あり {s['had_coords']}件）{prec}")
    print(f"  判断を反映: {summary['review_applied']}件 / 確認待ち: {summary['review_pending']}件")
    if summary["review_pending"]:
        print(f"  → Claude Code で /odkit-review を実行してください（{p.work / 'review' / 'queue.json'}）")
    print(f"README: {readme}")
    return 0


def cmd_status(cfg: dict) -> None:
    p = Pipeline(cfg)
    q = p.work / "review" / "queue.json"
    if not q.exists():
        print("まだ build していません")
        return
    data = json.loads(q.read_text("utf-8"))
    kinds: dict[str, int] = {}
    human = 0
    for it in data["items"]:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
        human += it.get("waiting_for") == "human"
    print(f"確認待ち {data['pending']}件 {kinds}（うち人の判断待ち {human}件）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="odkit", description="自治体オープンデータを標準形式に整える")
    ap.add_argument("command", choices=["fetch", "inspect", "build", "status"])
    ap.add_argument("config", type=Path)
    ap.add_argument("--offline", action="store_true", help="住所検索をキャッシュだけで行う")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.command == "fetch":
        cmd_fetch(cfg)
    elif a.command == "inspect":
        cmd_inspect(cfg)
    elif a.command == "build":
        return cmd_build(cfg, a.offline)
    else:
        cmd_status(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
