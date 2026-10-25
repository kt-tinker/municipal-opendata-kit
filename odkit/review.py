"""人（または AI）の判断が要る項目の「確認リスト」と、その「判断記録」を扱う。

- パイプラインは判断が要る項目を review/queue.json に書き出す
- Claude Code のスキル（.claude/skills/odkit-review）がそれを読み、review/decisions.json に判断を書く
- 次の build で判断を反映する。判断はコードで検証し、通らなければ確認リストに戻す

AI は「判断と提案」だけを行う。座標そのものは必ず元データか国土地理院の結果から取る。
"""

from __future__ import annotations

import json
from pathlib import Path

KINDS = {
    "geocode": "住所から高い精度で位置を付けられなかった",
    "name_match": "公共施設情報に似た名前の施設があるが、同じ施設か確信がない",
    "address_split": "住所を町字／番地以下／建物名等に分けられなかった",
    "mismatch": "同じ施設なのに、資料によって住所が違う",
}

ACTIONS = {
    "geocode": {"retry_address", "use_reference", "accept_town", "no_location", "needs_human"},
    "name_match": {"use_reference", "reject", "needs_human"},
    "address_split": {"split", "needs_human"},
    "mismatch": {"prefer_reference", "prefer_source", "needs_human"},
}


class Review:
    def __init__(self, review_dir: Path):
        self.dir = review_dir
        path = review_dir / "decisions.json"
        self.decisions: dict = json.loads(path.read_text("utf-8")).get("decisions", {}) if path.exists() else {}
        self.queue: list[dict] = []
        self.applied: list[dict] = []

    def decision(self, item_id: str, kind: str) -> dict | None:
        d = self.decisions.get(item_id)
        if not d or d.get("action") not in ACTIONS[kind] or d.get("action") == "needs_human":
            return None
        return d

    def ask(self, item_id: str, kind: str, context: dict, error: str | None = None) -> None:
        item = {"id": item_id, "kind": kind, "what": KINDS[kind], "allowed_actions": sorted(ACTIONS[kind]), "context": context}
        prev = self.decisions.get(item_id)
        if prev and prev.get("action") == "needs_human":
            item["waiting_for"] = "human"
            item["ai_note"] = prev.get("reason", "")
        if error:
            item["rejected_decision"] = {"decision": prev, "why": error}
        self.queue.append(item)

    def record(self, item_id: str, decision: dict) -> None:
        self.applied.append({"id": item_id, **decision})

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "queue.json").write_text(
            json.dumps({"pending": len(self.queue), "items": self.queue}, ensure_ascii=False, indent=1), "utf-8"
        )
        dec = self.dir / "decisions.json"
        if not dec.exists():
            dec.write_text(json.dumps({"decisions": {}}, ensure_ascii=False, indent=1), "utf-8")
