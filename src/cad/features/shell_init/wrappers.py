"""
features/shell_init/wrappers.py — the actual shell snippets.

One per supported shell. The mechanism is identical (run cad, read
the cwd file it wrote, cd the parent shell to that path); zsh and
bash just want slightly different conditional syntax.

``command cad`` in the wrapper makes the binary run once instead of
recursing into the function we're defining.
"""


SHELL_WRAPPERS = {
    "zsh": """\
cad() {
  local cwd_file
  cwd_file=$(mktemp -t cad-cwd.XXXXXX)
  CAD_CWD_FILE="$cwd_file" command cad "$@"
  local rc=$?
  if [[ -s "$cwd_file" ]]; then
    cd "$(< "$cwd_file")" || true
  fi
  rm -f "$cwd_file"
  return $rc
}
""",
    "bash": """\
cad() {
  local cwd_file
  cwd_file=$(mktemp -t cad-cwd.XXXXXX)
  CAD_CWD_FILE="$cwd_file" command cad "$@"
  local rc=$?
  if [ -s "$cwd_file" ]; then
    cd "$(cat "$cwd_file")" || true
  fi
  rm -f "$cwd_file"
  return $rc
}
""",
}
