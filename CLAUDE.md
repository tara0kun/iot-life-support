# iot-life-support — Claude Code 向けメモ

## 最初に読む

1. **`HANDOFF.md` と `PROGRESS.md`** — 現在の状態と経緯
2. **`~/claude-context/reviews/iot-life-support_risk-review_2026-10-29.md`** — 2026-10-29 のリスク棚卸し
   - 読む順: §0 結論 → §1 リスク一覧 → §4 文書間の食い違い
   - 一覧は `~/claude-context/reviews/README.md`。claude-context は private リポジトリにある
   - レビューは作成時点（`dev` `c9e7422`）の写真なので、今の HEAD と比べ、直っている指摘・古くなった指摘を確かめてから前提にする
   - 指摘を直したら、`reviews/README.md` の「状態」と「最終確認日」を更新する
3. **共有事項**: `python ~/claude-context/scripts/shared.py digest --cwd "$PWD"` の出力（ローカルでは起動時に自動表示される）。他のリポジトリにも効く事項が出たら `shared.py add` で足す。運用は `~/claude-context/shared/README.md`

## 守ること

- 本番の Raspberry Pi は `dev` ブランチで稼働している
- このリポジトリは public。家族の情報、接続情報、生活パターンは書かない（`docs/WORKFLOW.md`「公開範囲の注意」と `scripts/check_no_pii.py` に従う）
