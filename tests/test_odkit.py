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
        s = split_address("埼玉県草加市柿木町地内", "埼玉県", "草加市")
        self.assertEqual((s["所在地_町字"], s["所在地_番地以下"]), ("柿木町", "地内"))
        self.assertEqual(split_address("埼玉県越谷市中町", "埼玉県", "越谷市")["所在地_町字"], "中町")
        self.assertIsNone(split_address("埼玉県越谷市広場(埼玉県越谷市相模町1丁目)", "埼玉県", "越谷市"))

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


class TestDiscover(unittest.TestCase):
    """ポータルの HTML の形（2026年10月時点で確認したもの）を模して、探索の流れを確かめる。"""

    PAGES = {
        "/datasets?q=": '<li><a href="#" class="side_condition side_organization" data-type="organization" data-id="43">越谷市（3）</a></li>'
                        '<a href="#" data-type="organization" data-id="42">草加市（10）</a>',
        "/datasets?organization_id=43&page=1": '<a href="https://x/datasets/71"><h3>【越谷市】24時間使用可能なAED設置場所一覧</h3><p>説明</p></a>'
                                              '<a href="https://x/datasets/552">【越谷市】公共施設一覧</a><a href="https://x/datasets/531">【越谷市】公衆トイレ一覧</a>',
        "/datasets?organization_id=43&page=2": '<a href="https://x/datasets/71">【越谷市】24時間使用可能なAED設置場所一覧</a>',
        "/datasets/71": '<a href="https://x/resources/380">24時間使用可能なAED設置場所一覧</a>',
        "/datasets/552": '<a href="https://x/resources/2757">越谷市の公共施設一覧</a>',
        "/resources/380": '<dl><dt>ファイル名</dt><dd>24aed.csv</dd></dl><a>ダウンロード</a><td>最終更新</td><td>2024年09月27日</td>',
        "/resources/2757": '<dt>ファイル名</dt><dd>112224_publicfacility .csv</dd><a>ダウンロード</a><td>最終更新</td><td>2025年12月10日</td>',
    }

    def test_discover(self):
        import tempfile
        from unittest import mock
        from odkit import discover as dc

        def fake_get(path, interval=0.5):
            key = "/datasets?q=" if path.startswith("/datasets?q=") else path
            return self.PAGES.get(key, "")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(dc, "get", fake_get):
            r = dc.discover("越谷市", "koshigaya", Path(tmp))
            self.assertEqual(r["organization_id"], "43")
            self.assertEqual([c["dataset_id"] for c in r["candidates"]["aed"]], ["71"])
            self.assertEqual(r["candidates"]["public_facility"][0]["resources"][0]["file"], "112224_publicfacility .csv")
            self.assertNotIn("531", [c["dataset_id"] for c in r["candidates"]["public_facility"]])
            draft = (Path(tmp) / "cities" / "koshigaya.draft.toml").read_text("utf-8")
            self.assertIn("resource_download/380", draft)
            self.assertIn("# [datasets.cultural_property] … ポータルに見つかりませんでした", draft)


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
        self.assertNotIn("cultural_property:SOKA-CUL-002:address_split", ids)  # 「地内」は決まった形で分ける
        self.assertEqual(ids["aed:SOKA-AED-020:mismatch"]["waiting_for"], "human")
        self.assertEqual(s["review_applied"], 1)


if __name__ == "__main__":
    unittest.main()
