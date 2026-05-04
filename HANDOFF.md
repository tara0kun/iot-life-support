# 引き継ぎメモ（2026-04-22）

次回のセッションで以下を読んでから作業を開始してください:

```
PROGRESS.md を読んで続きを進めて。
```

---

## 現在のブランチ状態

| ブランチ | 状態 | 最新コミット |
|---------|------|-------------|
| **dev** | アクティブ（現在のブランチ） | `a3e28fc` PROGRESS.md記録 |
| **future** | Service Worker, 週間ダッシュボード等 | `3b2e369` |
| **main** | GW投入用（devのマージ待ち） | `6764a90`（古い） |

### 重要: dev → main マージが未実施
devブランチに安定した変更が大量にある。GW投入前にmainへマージが必要。

---

## systemd反映が必要

以下のファイルが更新されているがsystemdに未反映:
```bash
sudo cp ~/iot-life-support/systemd/iot-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart iot-web
```

---

## 4/20-21で実装した主な機能

### タブレット（祖母用）
- 「できた」ボタン（起床/お薬/お風呂/就寝）+ センサー照合 + LINE通知
- 30分クールダウン（連打防止）
- 花の色8種類（日ごとに異なる）
- 漢字表記に変更
- 家族からのメッセージ表示（「わかった」ボタンで非表示）
- 活動時間の指定を削除（「ご飯の時間」等は表示しない設計方針）
- 一般的な促し（「朝ごはんは食べましたか？」）は残す

### 家族管理画面
- 日付ナビゲーション（過去日のイベント閲覧）
- イベント時刻編集・削除
- かんたん記録の確認ダイアログ（誤タップ防止）
- 炊飯量設定（1合/2合/3合プリセット + 自由入力 + クリア）
- 薬スケジュール設定（朝/昼/夜の時刻を家族が設定）
- タブレットへの伝言機能（プリセット4種 + 自由入力）
- ソース名の日本語化
- 週間サマリー（futureブランチ）

### インフラ
- Git 3ブランチ運用（main/dev/future）
- iot-web再起動時にLINE URL通知
- Python 3.12+ datetime警告解消
- Service Worker（futureブランチ）

---

## GW投入までの残タスク（ハードウェア必要）

- [ ] 顔登録テスト
- [ ] H100/T110統合テスト
- [ ] 実際の炊飯器で電力しきい値確認
- [ ] 全センサー統合テスト
- [ ] dev → main マージ
- [ ] systemd反映

---

## 開発サーバの起動方法

```bash
cd ~/iot-life-support
source venv/bin/activate
uvicorn src.web.app:app --host 0.0.0.0 --port 8000 --reload
```

## モックデータ投入

```bash
python scripts/seed_mock_data.py --clear-all --days 5
```
