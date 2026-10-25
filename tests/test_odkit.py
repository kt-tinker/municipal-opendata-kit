"""python -m unittest discover tests"""

import csv
import json
import shutil
import unittest
from pathlib import Path

from odkit import geocode as geo
from odkit.normalize import canonical_address, split_address, split_count, to_decimal_degree, wareki_to_iso
from odkit.pipeline import Pipeline, load_config, render

TESTS = Path(__file__).resolve().parent
CONFIG = TESTS / "cities" / "soka-test.toml"


class TestNormalize(unittest.TestCase):
    def test_wareki(self):
        self.assertEqual(wareki_to_iso("昭和51.11.15"), "1976-11-15")
        self.assertEqual(wareki_to_iso("平成元年4月1日"), "1989-04-01")
        self.assertIsNone(wareki_to_iso("不明"))

    def test_degree(self):
        self.assertAlmostEqual(to_decimal_degree("35°49′30.0″"), 35.825, places=6)
        self.assertEqual(to_decimal_degree("139.8"), 139.8)

    def test_address(self):
        self.assertEqual(canonical_address("草加市高砂一丁目１番地１号"), "草加市高砂1丁目1-1")
        s = split_address("埼玉県草加市松原1丁目1-5 中央図書館", "埼玉県", "草加市")
        self.assertEqual((s["所在地_町字"], s["所在地_番地以下"], s["建物名等"]), ("松原1丁目", "1-5", "中央図書館"))
        self.assertIsNone(split_address("埼玉県草加市柿木町地内", "埼玉県", "草加市"))

    def test_count(self):
        self.assertEqual(split_count("1棟"), ("1", "棟"))

    def test_render(self):
        spec = "利用可能日：{日}／利用可能時間：{時間}"
        self.assertEqual(render(spec, {"日": "365日", "時間": "24時間"}), "利用可能日：365日／利用可能時間：24時間")
        self.assertEqual(render(spec, {"日": "", "時間": "24時間"}), "利用可能時間：24時間")
        self.assertEqual(render(spec, {}), "")

    def test_precision(self):
        j = lambda t: geo.judge_precision("埼玉県草加市高砂1丁目1-1", t, "埼玉県", "草加市")
        self.assertEqual(j("埼玉県草加市高砂一丁目１－１"), "address")
        self.assertEqual(j("埼玉県草加市高砂一丁目１"), "block")
        self.assertEqual(j("埼玉県草加市高砂一丁目"), "town")
        self.assertEqual(j("埼玉県八潮市中央一丁目"), "none")


class TestClean(unittest.TestCase):
    def test_koshigaya_style_values(self):
        from odkit.clean import clean_row
        r = {"文化財指定日": "42599", "開始時間": "0.25", "終了時間": "0.75", "所在地_市区町村": "越谷市野島32",
             "所在地_町字": "大沢３", "所在地_番地以下": "6ｰ1", "所在地_連結表記": "埼玉県越谷市大沢３－６－１",
             "法人番号": "2.03E+12", "電話番号": "(048)970-7411"}
        fixes = clean_row(r, "埼玉県", "越谷市")
        self.assertEqual(r["文化財指定日"], "2016-08-17")
        self.assertEqual((r["開始時間"], r["終了時間"]), ("06:00", "18:00"))
        self.assertEqual(r["所在地_市区町村"], "越谷市")
        self.assertEqual((r["所在地_町字"], r["所在地_番地以下"]), ("大沢3", "6-1"))
        self.assertEqual(r["所在地_連結表記"], "埼玉県越谷市大沢3-6-1")
        self.assertEqual(r["法人番号"], "")
        self.assertEqual(r["電話番号"], "048-970-7411")
        self.assertEqual(len(fixes), 9)


class TestPipeline(unittest.TestCase):
    def setUp(self):
        for d in ("work/soka-test", "output/soka-test"):
            shutil.rmtree(TESTS / d, ignore_errors=True)
        (TESTS / "work/soka-test").mkdir(parents=True)
        shutil.copy(TESTS / "fixtures/soka/geocode_cache.json", TESTS / "work/soka-test/geocode_cache.json")

    def build(self):
        return Pipeline(load_config(CONFIG), offline=True).run()

    def rows(self, ds):
        with open(TESTS / f"output/soka-test/112216_{ds}.csv", encoding="utf-8-sig", newline="") as f:
            return {r["ID"]: r for r in csv.DictReader(f)}

    def test_reproduces_manual_result(self):
        s = self.build()
        self.assertEqual(s["datasets"]["aed"]["precision"]["reference"], 79)
        self.assertEqual(s["datasets"]["aed"]["precision"]["address"], 29)
        self.assertEqual({m["name"] for m in s["mismatches"]}, {"中央図書館", "新田中学校"})
        cul = self.rows("cultural_property")["SOKA-CUL-001"]
        self.assertEqual(cul["文化財指定日"], "1976-11-15")
        self.assertIn("度分秒", cul["備考"])

    def test_decisions_are_applied_and_validated(self):
        self.build()
        dec = {"decisions": {
            # 正しい判断：候補の中の施設を選ぶ
            "aed:SOKA-AED-008:name_match": {"action": "use_reference", "name": "市役所", "by": "ai", "reason": "同じ住所の本庁舎"},
            # 候補にない施設名 → 採用されない
            "aed:SOKA-AED-013:name_match": {"action": "use_reference", "name": "存在しない施設", "by": "ai"},
            # つなげても元の住所に戻らない分割 → 採用されない
            "cultural_property:SOKA-CUL-002:address_split": {"action": "split", "by": "ai",
                "split": {"所在地_町字": "柿木町", "所在地_番地以下": "1-1", "建物名等": ""}},
            "aed:SOKA-AED-020:mismatch": {"action": "needs_human", "by": "ai", "reason": "どちらが正しいか市への確認が必要"},
        }}
        (TESTS / "work/soka-test/review/decisions.json").write_text(json.dumps(dec, ensure_ascii=False), "utf-8")
        s = self.build()
        aed = self.rows("aed")
        self.assertIn("SOKA-FAC-001", aed["SOKA-AED-008"]["備考"])
        q = json.loads((TESTS / "work/soka-test/review/queue.json").read_text("utf-8"))
        ids = {i["id"]: i for i in q["items"]}
        self.assertNotIn("aed:SOKA-AED-008:name_match", ids)
        self.assertIn("aed:SOKA-AED-013:name_match", ids)
        self.assertIn("rejected_decision", ids["cultural_property:SOKA-CUL-002:address_split"])
        self.assertEqual(ids["aed:SOKA-AED-020:mismatch"]["waiting_for"], "human")
        self.assertEqual(s["review_applied"], 1)


if __name__ == "__main__":
    unittest.main()
