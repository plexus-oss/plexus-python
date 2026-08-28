#!/bin/bash
# Usage: ./scripts/release.sh <version>
# Example: ./scripts/release.sh 0.4.8
#
# Extracts the matching section from CHANGELOG.md and creates a GitHub release.
# The publish workflow fires automatically and pushes to PyPI.

set -euo pipefail

VERSION=${1:-}
if [[ -z "$VERSION" ]]; then
    echo "Usage: $0 <version>  (e.g. $0 0.4.8)" >&2
    exit 1
fi

# Verify the version exists in CHANGELOG.md
if ! grep -q "## \[$VERSION\]" CHANGELOG.md; then
    echo "Error: no entry for [$VERSION] found in CHANGELOG.md" >&2
    exit 1
fi

# Verify pyproject.toml version matches
CODE_VERSION=$(python3 -c "
import re
with open('pyproject.toml') as f:
    m = re.search(r'^version\s*=\s*\"(.+?)\"', f.read(), re.MULTILINE)
    print(m.group(1))
")
if [[ "$CODE_VERSION" != "$VERSION" ]]; then
    echo "Error: pyproject.toml has version $CODE_VERSION, expected $VERSION" >&2
    exit 1
fi

# plexus/__init__.py carries its own __version__, and it is what users read
# back when you ask them what they are running. Keep it in lockstep.
INIT_VERSION=$(python3 -c "
import re
with open('plexus/__init__.py') as f:
    m = re.search(r'^__version__\s*=\s*\"(.+?)\"', f.read(), re.MULTILINE)
    print(m.group(1))
")
if [[ "$INIT_VERSION" != "$VERSION" ]]; then
    echo "Error: plexus/__init__.py has version $INIT_VERSION, expected $VERSION" >&2
    exit 1
fi

# Extract release title suffix (the part after the date, e.g. "Video input broadening and wire safety")
TITLE_SUFFIX=$(grep "## \[$VERSION\]" CHANGELOG.md | sed 's/.*[0-9] - //')

# Extract just the changelog section for this version
NOTES=$(python3 -c "
import re, sys
txt = open('CHANGELOG.md').read()
m = re.search(r'## \[$VERSION\].*?(?=\n## \[|\Z)', txt, re.DOTALL)
if not m:
    sys.exit(1)
sys.stdout.write(m.group(0).strip())
" VERSION="$VERSION")

echo "Creating release v$VERSION — $TITLE_SUFFIX"
echo "---"
echo "$NOTES"
echo "---"
read -p "Proceed? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Aborted." >&2
    exit 1
fi

gh release create "v$VERSION" \
    --title "$VERSION — $TITLE_SUFFIX" \
    --notes "$NOTES"

echo "Release v$VERSION created. PyPI publish workflow is running."
