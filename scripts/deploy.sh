#!/bin/bash
# Push the freshly generated output/ directory to the gh-pages branch.
# Used by the Claude Routine after running the pipeline.
#
# Assumes:
#   - We're at the repo root
#   - output/ has been freshly generated (digest.html + images/)
#   - git remote 'origin' is configured with push access
set -euo pipefail

if [[ ! -f "output/digest.html" ]]; then
  echo "ERROR: output/digest.html is missing — did the pipeline run?" >&2
  exit 1
fi

# Stage the output as the new gh-pages branch using git worktree
WORKTREE="/tmp/dad-tickets-pages-$$"
trap "rm -rf '$WORKTREE'" EXIT

# Ensure local refs are up-to-date
git fetch origin gh-pages 2>/dev/null || true

if git rev-parse --verify origin/gh-pages >/dev/null 2>&1; then
  git worktree add "$WORKTREE" origin/gh-pages
  cd "$WORKTREE"
  git checkout -B gh-pages
else
  git worktree add --orphan -b gh-pages "$WORKTREE"
  cd "$WORKTREE"
fi

# Wipe and replace
find . -maxdepth 1 ! -name '.git' ! -name '.' -exec rm -rf {} +

# Copy output → root, with index.html as the entry point
cp -r "$OLDPWD/output/"* .
mv digest.html index.html

# Friendly 404 → fall back to index
cat > 404.html <<'EOF'
<!DOCTYPE html>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0; url=./">
EOF

# Disable Jekyll (otherwise GitHub Pages strips files starting with _)
touch .nojekyll

git add -A
if git diff --cached --quiet; then
  echo "No changes to deploy."
  exit 0
fi
git -c user.name="Dad Tickets Bot" -c user.email="bot@dad-tickets.local" commit -m "Daily build $(date -u +%Y-%m-%d_%H:%M)"
git push origin gh-pages

echo "Deployed to gh-pages."
