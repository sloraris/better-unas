#!/bin/sh
# better-unas bootstrap. Run on the UNAS as root:
#
#   sh -c "$(curl -fsSL https://raw.githubusercontent.com/sloraris/better-unas/main/bootstrap.sh)"
#
# It checks the box, downloads the latest release to a temporary folder and
# opens the better-unas setup wizard from there. Nothing on the NAS changes
# until you confirm the wizard's review screen; quitting before that leaves
# the box exactly as it was. The wizard installs to /persistent/better-unas/,
# which survives firmware updates. On a box that's set up already it just
# opens better-unas.
#
# Options:
#   --ref <tag|branch>   use this version instead of the latest release
#                        (--ref main for the development version)
#   --dry-run            walk through the wizard and show the review; change nothing
#   --force              continue on a box that doesn't identify as a UNAS
#
# POSIX sh on purpose: it runs before anything else is known about the box.

set -eu

REPO="sloraris/better-unas"
HOME_DIR="${BETTER_UNAS_HOME:-/persistent/better-unas}"
REF="" DRY="" FORCE="" WORK=""

say() { printf '%s\n' "$*"; }
die() { printf 'better-unas: %s\n' "$*" >&2; exit 1; }

# With sh -c "$(curl ...)" --ref main, the shell takes the first word after
# the script as $0, not as an argument. Put it back where it belongs.
case "$0" in
  -*) [ "$0" = "-" ] || [ "$0" = "--" ] || set -- "$0" "$@" ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --) ;;
    --ref) [ $# -ge 2 ] || die "--ref needs a value"; REF=$2; shift ;;
    --ref=*) REF=${1#*=} ;;
    --dry-run) DRY=--dry-run ;;
    --force) FORCE=--force ;;
    -h|--help) sed -n '2,19s/^# \{0,1\}//p' "$0" 2>/dev/null || say "See https://github.com/$REPO"; exit 0 ;;
    *) die "unknown option $1 (try --help)" ;;
  esac
  shift
done

cleanup() { if [ -n "$WORK" ]; then rm -rf "$WORK"; fi; }
trap cleanup EXIT
trap 'exit 130' INT TERM

# --------------------------------------------------------------- preflight
# The wizard checks again in Python; these are the checks needed to get there.
if [ "${BETTER_UNAS_SKIP_CHECKS:-0}" != 1 ]; then
  if [ -z "$DRY" ]; then
    [ "$(id -u)" -eq 0 ] || die "run this as root (log in as root@<your UNAS>)"
    if ! grep -qi unas /usr/lib/version 2>/dev/null; then
      [ -n "$FORCE" ] || die "this doesn't look like a UniFi UNAS (checked /usr/lib/version); use --force to continue anyway"
      say "Warning: not identified as a UNAS; continuing because of --force."
    fi
  fi
  for t in python3 curl tar; do
    command -v "$t" >/dev/null 2>&1 || die "missing $t; this firmware isn't supported"
  done
  python3 -c 'import curses, sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null \
    || die "python3 is too old or has no curses; this firmware isn't supported"
fi

# --------------------------------------------------------------- download
if [ -n "${BETTER_UNAS_SRC:-}" ]; then
  # Development: a local checkout instead of a download.
  SRC=$BETTER_UNAS_SRC
  [ -f "$SRC/VERSION" ] && [ -d "$SRC/core" ] || die "BETTER_UNAS_SRC=$SRC isn't a better-unas checkout"
else
  if [ -z "$REF" ]; then
    REF=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
      | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1) || REF=""
    [ -n "$REF" ] || die "couldn't find a release of $REPO (none published yet, or GitHub unreachable). For the development version, add: --ref main"
  fi
  case "$REF" in *[!A-Za-z0-9._/-]*|*..*) die "invalid --ref $REF" ;; esac
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/better-unas.XXXXXX")
  say "Downloading better-unas $REF ..."
  curl -fsSL "https://github.com/$REPO/archive/$REF.tar.gz" \
    | tar -xz --strip-components=1 -C "$WORK" 2>/dev/null \
    || die "download failed for $REF (does that tag or branch exist?)"
  [ -f "$WORK/VERSION" ] && [ -d "$WORK/core" ] || die "$REF isn't a better-unas release with the setup wizard"
  SRC=$WORK
fi

# --------------------------------------------------------------- the wizard
# Run from the release folder so `python3 -m core.tui` is the downloaded code.
# Not exec: the trap above removes the temporary folder afterwards.
if [ ! -t 0 ]; then
  (exec </dev/tty) 2>/dev/null || die "the setup wizard needs a terminal; run this in an SSH session"
  exec </dev/tty
fi
# shellcheck disable=SC2086
(cd "$SRC" && python3 -m core.tui --home "$HOME_DIR" --source "$SRC" $DRY $FORCE)
