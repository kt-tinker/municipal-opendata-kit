# municipal-opendata-kit

- Python 3.11+、標準ライブラリのみ（Excel を読むときだけ openpyxl）
- テスト：`python -m unittest discover tests`
- 市を追加（通し作業）：`/odkit-add-city`。列の対応づけ：`/odkit-map-columns`、確認待ちの判断：`/odkit-review`
- ほかの AI に任せるときのプロンプト：docs/prompt.md
- 座標の数値を AI が書いてはいけない（docs/design.md「AI に任せないこと」）
- 標準の項目名は standards/ の定義と完全に一致させる
