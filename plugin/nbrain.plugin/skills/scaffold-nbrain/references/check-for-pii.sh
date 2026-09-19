#!/usr/bin/env bash
# Pre-packaging privacy gate for the nbrain plugin.
#
# Scans a directory for personal identifiers that must never ship to other users.
# Exit 0 = clean, safe to package. Exit 1 = leaks found, DO NOT package.
#
#   ./check-for-pii.sh <directory-to-scan>
#
# Two detection passes:
#   1. STRUCTURAL — patterns that are personal by construction regardless of whose data
#      it is (email addresses, home paths, ticket keys, tenant hostnames). These work
#      for any user, so this pass never needs editing.
#   2. NAMES — supplied at runtime, so this script itself stays free of personal data.
#      Pass them in NAMES_TO_CHECK, space separated:
#        NAMES_TO_CHECK="surname1 surname2" ./check-for-pii.sh ./dist

set -uo pipefail
DIR="${1:-.}"
FAIL=0

if [ ! -d "$DIR" ]; then echo "Not a directory: $DIR" >&2; exit 2; fi

echo "Scanning $DIR"
echo ""

# report <label> <pattern> [case]
#   case = "sensitive" for patterns where letter case is meaningful, such as an
#   all-caps ticket key followed by digits.
#   Default is insensitive. Getting this wrong causes false positives: an
#   insensitive [A-Z]{2,10}-[0-9]+ happily matches "Tier-1".
report() {
  local label="$1" pattern="$2" case="${3:-insensitive}"
  local flags="-rnE"
  [ "$case" = "insensitive" ] && flags="-rniE"
  local hits
  hits=$(grep $flags "$pattern" "$DIR" \
           --include='*.md' --include='*.json' --include='*.yaml' --include='*.yml' \
           --include='*.txt' --include='*.sh' --include='*.py' 2>/dev/null \
         | grep -v '{{' || true)
  if [ -n "$hits" ]; then
    echo "FAIL  $label"
    echo "$hits" | head -12 | sed 's/^/        /'
    local n; n=$(echo "$hits" | wc -l | tr -d ' ')
    [ "$n" -gt 12 ] && echo "        ... and $((n - 12)) more"
    echo ""
    FAIL=1
  else
    echo "ok    $label"
  fi
}

# --- Pass 1: structural. Personal by construction, user-agnostic. ---
report "email addresses"        '[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}'
report "home directory paths"   '/(Users|home)/[a-z0-9._-]+/'
report "Atlassian tenant URLs"  'https?://[a-z0-9-]+\.atlassian\.net'
report "ticket keys"            '\b[A-Z]{3,10}-[0-9]{2,6}\b' sensitive
report "Slack/Teams workspace"  'https?://[a-z0-9-]+\.slack\.com|teams\.microsoft\.com/l/'
report "chat permalinks"        'chat\.google\.com/(dm|room)/'
report "Drive/Docs file IDs"    'docs\.google\.com/(document|spreadsheets|presentation)/d/[A-Za-z0-9_-]{20,}'
report "repo paths with users"  '(gitlab|github)\.com/[^ ]*/users/[a-z0-9._-]+'
report "calendar event ids"     'eid=[A-Za-z0-9]{20,}'
report "gmail thread links"     'mail\.google\.com/mail/u/[0-9]+/#'

# --- Pass 2: names, supplied at runtime so this file stays clean. ---
if [ -n "${NAMES_TO_CHECK:-}" ]; then
  for n in $NAMES_TO_CHECK; do
    report "name: $n" "\\b$n\\b"
  done
else
  echo "warn  no NAMES_TO_CHECK supplied — name pass skipped."
  echo "      Structural checks alone will NOT catch a bare first name."
  echo "      Re-run with: NAMES_TO_CHECK=\"name1 name2\" $0 $DIR"
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "PASS — no personal identifiers found. Safe to package."
  echo "Reminder: a clean scan is necessary, not sufficient. Read the diff too."
  exit 0
else
  echo "BLOCKED — personal data found. Replace with {{PLACEHOLDER}} tokens before packaging."
  exit 1
fi
