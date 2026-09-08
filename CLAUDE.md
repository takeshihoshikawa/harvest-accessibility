# harvest-accessibility — プロジェクト指示

木材生産における集材距離を計算する QGIS Processing プラグイン。作業区域・林道・土場の3レイヤを入力とし、
各サンプル点からの集材距離（d1: 林道までの直線距離、d2: 林道網に沿った土場までの最短経路）と
どの土場に送るかを HTML レポートで出力する。現場での実用化に向けた開発・検証を進めている（連携先は vault の `projects/harvest-accessibility.md`「関係者」）。

使い方・パラメータ・出力の仕様は README.md を参照（外向けの静的な説明はそちらが正本）。

<!-- BEGIN GENERATED PROJECT STATUS -->
## Current Status

**Phase**: 現地実証を完了（2026-09-08）。既存・新規の2事業地でプラグインが落ちずに動作し合格。実証後の議論で伐倒モデルの仕様が固まり（伐倒方向の制約、根元と梢の近い方をつかむ動作のGIS表現）、DEM取得・単木ポイント対応・個体別の出力レイヤとあわせて実装し main へマージ済み。既存バグ2件（サンプル点の二重計上、空ジオメトリの土場の黙殺）も修正
**Next task**: 公開用リポジトリの運用を整える。private で運用し、タグを打つと公開用リポへ自動 push・release する GitHub Actions を組む
**Concern**: 実証地の一方の土場データに空ジオメトリの点があり、全木が残り1つの土場へ割り当てられていた。データ側の修正が要る
**Updated**: 2026-09-08
<!-- END GENERATED PROJECT STATUS -->

## マイルストーン

- v0.1.0 リリース・デモ完了（2026-04-15）✅
- リアルデータでのエラー修正（期限: 2026-04-22）
- プラグイン完成（目標: 2026年7月中旬）

## 構成

- `harvest_accessibility/`: プラグイン本体（QGIS Processing provider・アルゴリズム）
- `data/sample/`: 動作確認用のサンプルデータ。実測の調査データは `data/` 配下で gitignore 済み
- `CHANGELOG.md`: リリース履歴
