name: collect
on:
  schedule:
    - cron: "*/5 * * * *"   # 5分ごと（GitHubの混雑状況で数分遅れることがあります）
  workflow_dispatch:         # 手動実行ボタン
permissions:
  contents: write
concurrency:
  group: collect
  cancel-in-progress: false
jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - run: pip install -q jpholiday
      - run: python congestion.py
        env:
          FORCE: ${{ github.event_name == 'workflow_dispatch' }}
      - name: save
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898283+github-actions[bot]@users.noreply.github.com"
          git add -A
          git commit -q -m "collect $(TZ=Asia/Tokyo date +%F_%H%M)" || exit 0
          git pull -q --rebase || true
          git push -q
