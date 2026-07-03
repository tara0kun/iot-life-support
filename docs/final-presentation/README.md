# 最終発表用データ集約

> `iot-life-support` の最終発表 (IoTシステム開発Ⅱ 第3回、第15週 or 補講、要件未確認) 用に整理した参照データ。
> **資料作成は別環境 (Windows PC + PowerPoint 等) で行う** ため、そこから参照できるように公開可能な形にまとめる。
> **プライバシー**: 個人名 / 顔画像 / 家族固有の生活時間帯 / 認証情報 は一切含まない。

## 収録内容

| ファイル | 内容 | 想定用途 |
|---|---|---|
| [operational-stats.md](operational-stats.md) | DB 抽出の運用統計 (総件数・通知応答率・センサー別ボリューム・危険信号発火数・日別推移) | スライド「軸A: 運用データ分析」 |
| [incident-timeline.md](incident-timeline.md) | 5/15〜7/2 に発生した障害と恒久対策のタイムライン (コミットハッシュ付き) | スライド「軸A の実弾: 継続稼働性」「軸D: 技術的深化」 |
| [architecture.md](architecture.md) | 正しい 5層アーキテクチャ (センサ / GW判定 / 通知 / UI / データ) | スライド「軸D: 技術的深化」 (進捗発表で混同した箇所の是正) |
| [codebase-metrics.md](codebase-metrics.md) | LOC / モジュール数 / cron 数 / LINE コマンド数 / デバイス構成 | スライド全体で数字として引用 |

## 再抽出したい場合

`operational-stats.md` は自動生成。DB が更新されたら以下を実行して再生成:

```bash
cd /home/tara0/IoT
venv/bin/python scripts/extract_presentation_stats.py
```

出力先: `docs/final-presentation/operational-stats.md`

## 発表側との連携

- 発表資料 (PPT) は **Windows PC の `d:/IoTシステム/個人企画/`** で作成
- そちらから参照する場合は **GitHub の `iot-life-support` リポジトリの `docs/final-presentation/`** を見る (このディレクトリ)
- 元データが必要な場合は同ディレクトリの Markdown をコピーして PPT に貼る

## 発表戦略のリマインダ

過去2回の発表で話した内容 (絶対に重複させない):

- ❌ CPSサイクル図の詳細
- ❌ 「監視ではなく記録」UX の詳細
- ❌ 多層防御 3段階の詳細解説
- ❌ LINE 双方向 7コマンドの表 (今 18 コマンドあるので差分のみ)
- ❌ GW投入 7項目チェック

新規で埋める軸 (このディレクトリの材料で対応):

- ✅ **軸A**: 運用データ分析 → [operational-stats.md](operational-stats.md)
- ✅ **軸D**: 技術的深化 → [architecture.md](architecture.md) + [codebase-metrics.md](codebase-metrics.md)
- ✅ **災害復旧の実話** → [incident-timeline.md](incident-timeline.md)

軸B (Phase 2 実装成果) と 軸C (エコシステム化) と 軸E (家族フィードバック) は別途、実装と対話が進んでから追加。

詳細な戦略は claude-context リポジトリの [`courses/iput-isd2.md`](https://github.com/tara0kun/claude-context) 参照 (プライベート)。
