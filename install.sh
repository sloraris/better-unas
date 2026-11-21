#!/bin/sh
# better-unas installer. Run on the UNAS as root:
#
#   sh -c "$(curl -fsSL https://raw.githubusercontent.com/sloraris/better-unas/main/install.sh)"
#
# It downloads the latest release, asks which tools you want, copies them to
# /persistent/better-unas/<tool>/ and runs each tool's own install.sh. It keeps
# a copy of itself at /persistent/better-unas/better-unas for later use:
#
#   better-unas [install]           choose tools to install or update
#   better-unas update              update every installed tool
#   better-unas list                show available and installed tools
#   better-unas uninstall <tool>    run that tool's uninstall.sh
#
# Options:
#   --ref <tag|branch>   download this version instead of the latest release
#   --tools a,b          pick tools without the menu
#   --yes                don't ask (needs --tools for install)
#   --dry-run            show what would happen, change nothing
#   --purge              with uninstall: also delete the tool's config and state
#   --force              continue on a box that doesn't identify as a UNAS;
#                        with update: re-install tools that are already current
#
# POSIX sh on purpose: it has to run before anything else is known about the box.

set -eu

REPO="sloraris/better-unas"
HOME_DIR="${BETTER_UNAS_HOME:-/persistent/better-unas}"
SELF_NAME="better-unas"

CMD=install REF="" TOOLS="" YES=0 DRY=0 PURGE=0 FORCE=0 TARGET=""
SRC="" WORK="" AVAILABLE="" SELECTED="" DONE_OK="" DONE_FAILED="" REPLY=""

say() { printf '%s\n' "$*"; }
die() { printf 'better-unas: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,25s/^# \{0,1\}//p' "$0" 2>/dev/null || say "See https://github.com/$REPO"; }

while [ $# -gt 0 ]; do
  case "$1" in
    install|update|list|uninstall) CMD=$1 ;;
    --ref) [ $# -ge 2 ] || die "--ref needs a value"; REF=$2; shift ;;
    --ref=*) REF=${1#*=} ;;
    --tools) [ $# -ge 2 ] || die "--tools needs a value"; TOOLS=$2; shift ;;
    --tools=*) TOOLS=${1#*=} ;;
    -y|--yes) YES=1 ;;
    --dry-run) DRY=1 ;;
    --purge) PURGE=1 ;;
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option $1 (try --help)" ;;
    *)
      if [ "$CMD" = uninstall ] && [ -z "$TARGET" ]; then TARGET=$1
      else die "unexpected argument $1 (try --help)"; fi ;;
  esac
  shift
done

# ----------------------------------------------------------------- helpers

cleanup() { if [ -n "$WORK" ]; then rm -rf "$WORK"; fi; }
trap cleanup EXIT
trap 'exit 130' INT TERM

valid_name() {
  case "$1" in ""|*[!a-z0-9-]*) return 1 ;; esac
}

# conf_get FILE KEY: read KEY=VALUE without executing anything.
conf_get() { sed -n "s/^$2=//p" "$1" 2>/dev/null | head -n 1; }

has_word() {  # has_word WORD LIST...
  w=$1; shift
  for x in "$@"; do [ "$x" = "$w" ] && return 0; done
  return 1
}

nth() {  # nth N LIST...
  n=$1; shift; i=1
  for x in "$@"; do
    if [ "$i" -eq "$n" ]; then printf '%s' "$x"; return 0; fi
    i=$((i + 1))
  done
}

ask() {
  if ! (exec </dev/tty) 2>/dev/null; then
    die "no terminal to ask on; pass --tools and --yes"
  fi
  printf '%s ' "$1" >/dev/tty
  IFS= read -r REPLY </dev/tty || REPLY=""
}

confirm() {
  [ "$YES" -eq 1 ] && return 0
  ask "$1 [y/N]"
  case "$REPLY" in y|Y|yes|Yes|YES) return 0 ;; *) return 1 ;; esac
}

preflight() {
  if [ "${BETTER_UNAS_SKIP_CHECKS:-0}" = 1 ]; then return 0; fi
  [ "$(id -u)" -eq 0 ] || die "run this as root"
  if ! grep -qi unas /usr/lib/version 2>/dev/null; then
    [ "$FORCE" -eq 1 ] || die "this doesn't look like a UNAS (checked /usr/lib/version); use --force to continue anyway"
    say "Warning: not identified as a UNAS; continuing because of --force."
  fi
  for t in curl tar python3 sed find; do
    command -v "$t" >/dev/null 2>&1 || die "missing $t; this firmware isn't supported"
  done
  [ -d "${HOME_DIR%/*}" ] || die "${HOME_DIR%/*} not found; this firmware isn't supported"
}

latest_release() {
  curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
    | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
}

fetch() {
  if [ -n "${BETTER_UNAS_SRC:-}" ]; then   # development: use a local checkout
    SRC=$BETTER_UNAS_SRC; REF=${REF:-local}
  else
    if [ -z "$REF" ]; then
      REF=$(latest_release) || REF=""
      [ -n "$REF" ] || die "couldn't find a release of $REPO (none published yet, or GitHub unreachable). For the development version, add: --ref main"
    fi
    case "$REF" in ""|*[!A-Za-z0-9._/-]*) die "invalid --ref $REF" ;; esac
    WORK=$(mktemp -d "${TMPDIR:-/tmp}/better-unas.XXXXXX")
    say "Downloading better-unas $REF ..."
    curl -fsSL "https://github.com/$REPO/archive/$REF.tar.gz" \
      | tar -xz --strip-components=1 -C "$WORK" 2>/dev/null \
      || die "download failed for $REF (does that tag or branch exist?)"
    SRC=$WORK
  fi
  AVAILABLE=""
  for conf in "$SRC"/*/tool.conf; do
    [ -f "$conf" ] || continue
    dir=${conf%/tool.conf}; dir=${dir##*/}
    name=$(conf_get "$conf" NAME)
    if valid_name "$name" && [ "$name" = "$dir" ] && [ -f "$SRC/$dir/install.sh" ]; then
      AVAILABLE="$AVAILABLE $name"
    fi
  done
  [ -n "$AVAILABLE" ] || die "no tools found in $REF"
}

installed_tools() {
  for conf in "$HOME_DIR"/*/tool.conf; do
    [ -f "$conf" ] || continue
    dir=${conf%/tool.conf}; printf '%s ' "${dir##*/}"
  done
}

installed_version() { conf_get "$HOME_DIR/$1/tool.conf" VERSION; }

show_tools() {
  say ""
  say "better-unas $REF"
  i=1
  for name in $AVAILABLE; do
    ver=$(conf_get "$SRC/$name/tool.conf" VERSION)
    summary=$(conf_get "$SRC/$name/tool.conf" SUMMARY)
    have=$(installed_version "$name")
    if [ -z "$have" ]; then state="not installed"
    elif [ "$have" = "$ver" ]; then state="installed $have"
    else state="installed $have -> $ver"; fi
    printf '  [%d] %-18s %-52s %s\n' "$i" "$name" "$summary" "$state"
    i=$((i + 1))
  done
  for name in $(installed_tools); do
    has_word "$name" $AVAILABLE || printf '      %-18s %-52s %s\n' "$name" "(not in this release)" "installed $(installed_version "$name")"
  done
  say ""
}

add_selected() {
  has_word "$1" $SELECTED || SELECTED="$SELECTED $1"
}

select_tools() {
  if [ -n "$TOOLS" ]; then
    for t in $(printf '%s' "$TOOLS" | tr ',' ' '); do
      has_word "$t" $AVAILABLE || die "no tool named $t in $REF"
      add_selected "$t"
    done
    return 0
  fi
  [ "$YES" -eq 1 ] && die "--yes needs --tools to know what to install"
  ask "Install or update which? (numbers or names, \"all\", or Enter to cancel):"
  case "$REPLY" in *[!a-z0-9,\ -]*) die "didn't understand \"$REPLY\"" ;; esac
  for w in $(printf '%s' "$REPLY" | tr ',' ' '); do
    case "$w" in
      all) for t in $AVAILABLE; do add_selected "$t"; done ;;
      *[!0-9]*) has_word "$w" $AVAILABLE || die "no tool named $w"; add_selected "$w" ;;
      *) t=$(nth "$w" $AVAILABLE); [ -n "$t" ] || die "no tool number $w"; add_selected "$t" ;;
    esac
  done
}

install_self() {
  if [ "$DRY" -eq 1 ]; then say "(dry-run) would save the installer as $HOME_DIR/$SELF_NAME"; return 0; fi
  mkdir -p "$HOME_DIR"
  cp "$SRC/install.sh" "$HOME_DIR/$SELF_NAME.tmp"
  chmod 755 "$HOME_DIR/$SELF_NAME.tmp"
  mv "$HOME_DIR/$SELF_NAME.tmp" "$HOME_DIR/$SELF_NAME"
  printf '%s\n' "$REF" > "$HOME_DIR/VERSION"
}

install_tool() {
  name=$1; src="$SRC/$name"; dest="$HOME_DIR/$name"
  valid_name "$name" || die "bad tool name $name"
  setup=$(conf_get "$src/tool.conf" SETUP)
  if [ ! -f "$dest/tool.conf" ] && [ -n "$setup" ]; then
    say ""
    say "$name needs a step in the UniFi UI first:"
    say "  $setup"
    if [ "$YES" -eq 1 ]; then
      say "  (--yes: assuming it's done)"
    elif ! confirm "Done?"; then
      say "Skipping $name. Run better-unas again once it's done."
      return 0
    fi
  fi
  if [ "$DRY" -eq 1 ]; then
    say "(dry-run) would copy $name $(conf_get "$src/tool.conf" VERSION) to $dest and run its install.sh"
    return 0
  fi
  mkdir -p "$dest"
  # Replace the tool's own files; keep what belongs to this install.
  find "$dest" -mindepth 1 -maxdepth 1 ! -name config ! -name state.json ! -name .lock -exec rm -rf {} +
  cp -R "$src/." "$dest/"
  say ""
  say "== $name: running install.sh"
  if (cd "$dest" && sh ./install.sh); then
    DONE_OK="$DONE_OK $name"
  else
    DONE_FAILED="$DONE_FAILED $name"
  fi
}

summary() {
  say ""
  [ -n "$DONE_OK" ] && say "Installed:$DONE_OK"
  [ -n "$DONE_FAILED" ] && say "Problems (see output above):$DONE_FAILED"
  if [ -n "$DONE_OK$DONE_FAILED" ]; then
    say "Manage later with: $HOME_DIR/$SELF_NAME [update|list|uninstall <tool>]"
  fi
  [ -z "$DONE_FAILED" ]
}

# ----------------------------------------------------------------- commands

cmd_install() {
  preflight
  fetch
  show_tools
  select_tools
  if [ -z "$SELECTED" ]; then say "Nothing selected."; return 0; fi
  install_self
  for t in $SELECTED; do install_tool "$t"; done
  summary
}

cmd_update() {
  preflight
  have=$(installed_tools)
  [ -n "$have" ] || { say "No tools installed yet. Run: $HOME_DIR/$SELF_NAME install"; return 0; }
  fetch
  for name in $have; do
    if ! has_word "$name" $AVAILABLE; then
      say "$name: not in $REF; leaving it as it is."
      continue
    fi
    old=$(installed_version "$name"); new=$(conf_get "$SRC/$name/tool.conf" VERSION)
    if [ "$old" = "$new" ] && [ "$FORCE" -eq 0 ]; then
      say "$name: up to date ($old)."
    else
      say "$name: $old -> $new"
      add_selected "$name"
    fi
  done
  if [ -z "$SELECTED" ]; then install_self; say "Everything is up to date."; return 0; fi
  confirm "Update$SELECTED?" || { say "Cancelled."; return 0; }
  install_self
  for t in $SELECTED; do install_tool "$t"; done
  summary
}

cmd_list() {
  if [ -z "${BETTER_UNAS_SRC:-}" ] && ! command -v curl >/dev/null 2>&1; then
    die "curl is missing"
  fi
  fetch
  show_tools
}

cmd_uninstall() {
  preflight
  valid_name "$TARGET" || die "which tool? usage: $SELF_NAME uninstall <tool> [--purge]"
  dest="$HOME_DIR/$TARGET"
  [ -f "$dest/uninstall.sh" ] || die "$TARGET isn't installed (no $dest/uninstall.sh)"
  if [ "$PURGE" -eq 1 ]; then what="Uninstall $TARGET and delete its config and state?"
  else what="Uninstall $TARGET? (config is kept; add --purge to delete it)"; fi
  confirm "$what" || { say "Cancelled."; return 0; }
  if [ "$DRY" -eq 1 ]; then say "(dry-run) would run $dest/uninstall.sh"; return 0; fi
  if [ "$PURGE" -eq 1 ]; then
    (cd "$dest" && sh ./uninstall.sh --purge)
  else
    (cd "$dest" && sh ./uninstall.sh)
    # Mark it uninstalled so "update" won't quietly turn it back on.
    if [ -f "$dest/tool.conf" ]; then mv "$dest/tool.conf" "$dest/tool.conf.uninstalled"; fi
  fi
}

case "$CMD" in
  install) cmd_install ;;
  update) cmd_update ;;
  list) cmd_list ;;
  uninstall) cmd_uninstall ;;
esac
