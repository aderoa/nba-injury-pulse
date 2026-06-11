name: Poll NBA injury report

on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch: {}

permissions:
  contents: write

concurrency:
  group: injury-poll
  cancel-in-progress: false

jobs:
  poll:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install deps
        run: pip install pdfplumber --quiet

      - name: Poll
        run: python scripts/poll_injuries.py

      - name: Commit & push if changed
        run: |
          git config user.name "injury-pulse-bot"
          git config user.email "actions@users.noreply.github.com"
          git add data
          if git diff --cached --quiet; then
            echo "No changes."
          else
            git commit -m "Injury report update $(date -u +'%F %H:%M')"
            git push
          fi
