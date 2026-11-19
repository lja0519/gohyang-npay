#!/bin/bash
# 보고서를 다시 만들고, 바뀐 내용이 있으면 커밋해 사내 GitHub Pages 에 반영한다.
set -euo pipefail
cd "$(dirname "$0")"

git pull -q --ff-only
python3 gohyang_npay.py --html docs/index.html > /dev/null

git add docs state overrides.json
if git diff --cached --quiet; then
  echo "변경 없음"
  exit 0
fi
git commit -q -m "report: $(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M KST')"
git push -q
echo "게시 완료"
