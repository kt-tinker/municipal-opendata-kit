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


class TestGeocoderFailure(unittest.TestCase):
    """住所検索が応答しなくても build 全体は止めず、そこまでの結果を残す。"""

    def test_timeout_does_not_stop(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            g = geo.Geocoder(Path(tmp) / "cache.json", interval=0, log=lambda *_: None)
            g.cache["埼玉県川口市青木2丁目1-1"] = []
            with mock.patch.object(geo.Geocoder, "RETRY_WAITS", (0,)), \
                 mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
                self.assertIsNone(g.search("埼玉県川口市芝下1丁目11"))
                self.assertIsNone(g.search("埼玉県川口市芝1丁目1"))
                self.assertIsNone(g.search("埼玉県川口市並木2丁目1"))
                self.assertTrue(g.gave_up)          # 3件続けて失敗 → 残りは検索しない
                self.assertEqual(g.search("埼玉県川口市青木2丁目1-1"), [])  # キャッシュは使える
            self.assertEqual(g.failed, 3)
            self.assertTrue((Path(tmp) / "cache.json").exists())


class TestTownMaster(unittest.TestCase):
    """町字マスター（アドレス・ベース・レジストリ）で丁目を判定する。fixtures は川口市の町字マスターの抜粋。"""

    def setUp(self):
        from odkit.townmaster import TownMaster
        self.tm = TownMaster.load(TESTS / "fixtures/kawaguchi/mt_town_city112038_excerpt.csv")

    def split(self, a):
        s = self.tm.split(a, "埼玉県", "川口市")
        return s and (s["所在地_町字"], s["所在地_番地以下"], s["建物名等"], s["町字ID"])

    def test_split(self):
        self.assertEqual(self.split("埼玉県川口市青木2-1-1"), ("青木2丁目", "1-1", "", "0071002"))
        self.assertEqual(self.split("埼玉県川口市中青木1-2"), ("中青木1丁目", "2", "", "0013001"))
        self.assertEqual(self.split("埼玉県川口市芝下1-11"), ("芝下1丁目", "11", "", "0036001"))  # 「芝」より長い「芝下」
        self.assertEqual(self.split("埼玉県川口市鳩ヶ谷本町1‐6‐3"), ("鳩ヶ谷本町1丁目", "6-3", "", "0014001"))
        self.assertEqual(self.split("埼玉県川口市神戸767-1"), ("大字神戸", "767-1", "", "0093000"))  # 丁目のない町・大字の省略
        self.assertEqual(self.split("埼玉県川口市並木元町1-1"), ("並木元町", "1-1", "", "0077000"))
        self.assertEqual(self.split("埼玉県川口市河原町地内"), ("河原町", "地内", "", "0089000"))
        self.assertEqual(self.split("埼玉県川口市並木2-地内"), ("並木2丁目", "地内", "", "0045002"))
        self.assertEqual(self.split("埼玉県川口市飯塚"), ("飯塚", "", "", ""))
        self.assertEqual(self.split("埼玉県川口市青木二丁目1番1号 本館"), ("青木2丁目", "1-1", "本館", "0071002"))
        self.assertEqual(self.split("埼玉県川口市赤井1-2-3"), ("赤井1丁目", "2-3", "", "0024001"))
        self.assertEqual(self.split("埼玉県川口市赤井525‐1"), ("大字赤井", "525-1", "", "0090000"))  # 丁目にない番号→同名の大字
        self.assertEqual(self.split("埼玉県川口市赤井1-2地内"), ("赤井1丁目", "2地内", "", "0024001"))
        self.assertEqual(self.split("埼玉県川口市大字神戸70-1、74-9"), ("大字神戸", "70-1、74-9", "", "0093000"))
        self.assertIsNone(self.split("埼玉県川口市青木9-1-1"))   # 存在しない丁目
        self.assertIsNone(self.split("埼玉県川口市存在しない町1-1"))

    def test_compare_key(self):
        from odkit.townmaster import compare_key
        self.assertEqual(compare_key("西川口1丁目外"), compare_key("西川口1 外"))
        self.assertEqual(compare_key("大字新井宿保坂山306"), compare_key("新井宿保坂山306"))
        self.assertEqual(compare_key("青木2丁目1-1"), compare_key("青木2-1-1"))
        self.assertEqual(compare_key("栄町2丁目地内"), compare_key("栄町2-地内"))

    def test_town_id(self):
        self.assertEqual(self.tm.town_id("青木2"), ("青木2丁目", "0071002"))
        self.assertEqual(self.tm.town_id("赤山"), ("大字赤山", "0098000"))
        self.assertIsNone(self.tm.town_id("青木"))

    def test_outside_city(self):
        from odkit.normalize import outside_city
        self.assertTrue(outside_city("千葉県富津市千種新田755-1", "埼玉県", "川口市"))
        self.assertTrue(outside_city("埼玉県蕨市中央1-1", "埼玉県", "川口市"))
        self.assertFalse(outside_city("埼玉県川口市青木2-1-1", "埼玉県", "川口市"))
        self.assertFalse(outside_city("青木2-1-1", "埼玉県", "川口市"))


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

    def test_kawaguchi_style_values(self):
        from odkit.clean import clean_row
        r = {"文化財指定日": "2018/12/25", "開始時間": "9:00", "電話番号": "(048)-259-7651", "連絡先電話番号": " (048)285-2424",
             "所在地_連結表記": "埼玉県川口市川口市西川口5丁目2-1"}
        clean_row(r, "埼玉県", "川口市")
        self.assertEqual(r["所在地_連結表記"], "埼玉県川口市西川口5丁目2-1")
        self.assertEqual((r["文化財指定日"], r["開始時間"]), ("2018-12-25", "09:00"))
        self.assertEqual((r["電話番号"], r["連絡先電話番号"]), ("048-259-7651", "048-285-2424"))
        r = {"所在地_連結表記": "埼玉県川口市千葉県富津市千種新田755-1"}
        clean_row(r, "埼玉県", "川口市")
        self.assertEqual(r["所在地_連結表記"], "千葉県富津市千種新田755-1")


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

    def test_link_resource(self):
        """川口市のように、市のサイトへのリンクとして登録されているリソース。"""
        from odkit import discover as dc
        page = dc.parse('<dt>リソース名</dt><dd>【川口市】AED設置箇所一覧</dd><span>URL</span>'
                        '<a href="https://www.city.kawaguchi.lg.jp/material/files/group/1/112038_aed.csv">https://www.city.kawaguchi.lg.jp/material/files/group/1/112038_aed.csv</a>'
                        '<td>最終更新</td><td>2020年09月03日</td>')
        info = dc.resource_info(page)
        self.assertEqual(info["url"], "https://www.city.kawaguchi.lg.jp/material/files/group/1/112038_aed.csv")
        self.assertEqual(info["file"], "112038_aed.csv")


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


class TestKawaguchiStyle(unittest.TestCase):
    """すでに標準の列名だが、丁目を省いた住所・座標のない公共施設・市外の施設を含むデータ（川口市の形）。"""

    def test_build(self):
        for d in ("work/kawaguchi-test", "output/kawaguchi-test"):
            shutil.rmtree(TESTS / d, ignore_errors=True)
        s = Pipeline(load_config(TESTS / "cities/kawaguchi-test.toml"), offline=True).run()
        self.assertTrue(s["town_master"])
        self.assertEqual(s["datasets"]["public_facility"]["total"], 1)  # 名称のない行は除外
        self.assertIn("名称のない行を除外", {f["why"] for f in s["fixes"]})
        with open(TESTS / "output/kawaguchi-test/112038_aed.csv", encoding="utf-8-sig", newline="") as f:
            aed = {r["ID"]: r for r in csv.DictReader(f)}
        with open(TESTS / "output/kawaguchi-test/112038_public_facility.csv", encoding="utf-8-sig", newline="") as f:
            fac = next(csv.DictReader(f))
        self.assertEqual(fac["多目的トイレ"], "有")  # 以前の版の列名「バリアフリートイレ」を読み替え
        a = aed["k010000001"]
        self.assertEqual((a["所在地_町字"], a["所在地_番地以下"], a["町字ID"]), ("青木2丁目", "1-1", "0071002"))
        self.assertEqual((a["所在地_市区町村"], a["電話番号"], a["法人番号"], a["開始時間"]), ("川口市", "048-258-1249", "", "09:00"))
        out = aed["k010000094"]  # 市外の施設：座標は元データのまま、住所も書き換えない
        self.assertEqual((out["緯度"], out["所在地_連結表記"]), ("35.29159687", "千葉県富津市千種新田755-1"))
        self.assertIn("市外にある施設", out["備考"])
        self.assertEqual([o["id"] for o in s["outside"]], ["k010000094"])
        q = {i["id"]: i for i in json.loads((TESTS / "work/kawaguchi-test/review/queue.json").read_text("utf-8"))["items"]}
        self.assertEqual(q["aed:k010000121:geocode"]["context"]["searched"], "埼玉県川口市河原町")
        self.assertEqual(q["public_facility:k120000001:geocode"]["context"]["searched"], "埼玉県川口市青木2丁目1-1")


class TestSaitamaStyle(unittest.TestCase):
    def test_split_wards(self):
        from odkit.townmaster import TownMaster
        tm = TownMaster.load(TESTS / "fixtures/saitama/mt_town_wards_excerpt.csv")
        sp = lambda a: (lambda x: x and (x["区"], x["所在地_町字"], x["所在地_番地以下"], x["町字ID"]))(tm.split(a, "埼玉県", "さいたま市"))
        self.assertEqual(sp("埼玉県さいたま市北区土呂町352番"), ("北区", "土呂町", "352", "0008000"))      # 丁目にない番号
        self.assertEqual(sp("埼玉県さいたま市北区土呂町1-51-8"), ("北区", "土呂町1丁目", "51-8", "0008001"))
        self.assertIsNone(sp("埼玉県さいたま市北区土呂町1-42"))                                          # 丁目か番地か決められない
        self.assertEqual(sp("埼玉県さいたま市西区大字飯田新田字荒工250-15"), ("西区", "大字飯田新田", "字荒工250-15", "0019000"))
        self.assertEqual(sp("埼玉県さいたま市浦和区大字高砂4丁目3-18"), ("浦和区", "高砂4丁目", "3-18", "0004004"))
        self.assertEqual(sp("埼玉県さいたま市浦和区東高砂町無番地"), ("浦和区", "東高砂町", "無番地", "0016000"))
        self.assertEqual(sp("埼玉県さいたま市緑区大字大間木3丁目30番地11"), ("緑区", "大間木3丁目", "30-11", "0036003"))
        self.assertEqual(sp("埼玉県さいたま市緑区大字大間木100"), ("緑区", "大字大間木", "100", "0022000"))
        self.assertEqual(sp("埼玉県さいたま市北区土呂町"), ("北区", "土呂町", "", "0008000"))  # 混在する町の町名だけ
        self.assertIsNone(sp("埼玉県さいたま市高砂4丁目3-18"))  # 区がない

    """政令指定都市（区ごとの団体コード・町字マスター）と、独自形式の列のデータ（さいたま市の形）。"""

    def test_build(self):
        for d in ("work/saitama-test", "output/saitama-test"):
            shutil.rmtree(TESTS / d, ignore_errors=True)
        s = Pipeline(load_config(TESTS / "cities/saitama-test.toml"), offline=True).run()

        def rows(ds):
            with open(TESTS / f"output/saitama-test/111007_{ds}.csv", encoding="utf-8-sig", newline="") as f:
                return list(csv.DictReader(f))

        fac = rows("public_facility")[0]
        self.assertEqual((fac["所在地_市区町村"], fac["所在地_全国地方公共団体コード"]), ("さいたま市浦和区", "111074"))
        self.assertEqual((fac["所在地_町字"], fac["所在地_番地以下"], fac["町字ID"]), ("東高砂町", "11-1", "0016000"))
        self.assertEqual(fac["開始時間"], "09:00")
        aed = rows("aed")
        self.assertEqual(aed[0]["所在地_連結表記"], "埼玉県さいたま市西区飯田新田189-2 馬宮西小学校内")  # 区の列を補った
        self.assertEqual((aed[0]["所在地_町字"], aed[0]["所在地_番地以下"], aed[0]["建物名等(方書)"], aed[0]["町字ID"]),
                         ("大字飯田新田", "189-2", "馬宮西小学校内", "0019000"))
        self.assertEqual((aed[0]["所在地_市区町村"], aed[0]["所在地_全国地方公共団体コード"]), ("さいたま市西区", "111015"))
        self.assertEqual(aed[1]["所在地_連結表記"], "群馬県利根郡みなかみ町相俣159-1")  # 「市外」は区を補わない
        cul = rows("cultural_property")
        self.assertEqual((cul[0]["所在地_町字"], cul[0]["所在地_番地以下"]), ("大字田島", "他"))
        self.assertEqual((cul[1]["所在地_町字"], cul[1]["所在地_番地以下"], cul[1]["町字ID"]), ("高砂4丁目", "3-18", "0004004"))
        ev = rows("evacuation_space")[0]
        self.assertEqual((ev["災害種別_地震"], ev["災害種別_洪水"], ev["災害種別_大規模な火事"], ev["指定避難所との重複"]), ("1", "1", "", "1"))
        self.assertIn("災害種別_洪水：2F以上", ev["備考"])
        self.assertEqual((ev["所在地_町字"], ev["町字ID"]), ("西大宮1丁目", "0031001"))
        self.assertEqual(len(s["outside"]), 2)


if __name__ == "__main__":
    unittest.main()
