#!/bin/bash
# ==============================================================================
# A real, persistent bash shell -- the sandbox's second session, alongside the
# Python kernel. Both run in /workspace and share the VM's filesystem.
#
# This shell holds variables, exports, functions and working directory across
# commands and across suspend/resume, because AWS checkpoints the VM's memory
# -- there is no save path, no serialization and no replay. A `cd` or `export`
# in one run_shell call is still in effect in the next.
#
# Adapted from aws-lambda-microvms (01-microvms/bash/worker.sh).
#
# The VM is the security boundary, not this shell. Commands run through eval
# with full access to the process, which is acceptable only because the
# MicroVM around it is isolated and its execution role grants nothing.
#
# NOT `set -euo pipefail`, and that is deliberate -- it is the one place in this
# project where the house rule is wrong. Under `-e` a single failing cell would
# kill the shell and take the whole session with it, so a failed command has to
# be reported as data instead. `-u` and `-o pipefail` are omitted for the same
# reason: they would silently change the semantics of submitted code.
# ==============================================================================

# Every internal name is prefixed. Submitted code shares this shell, so an
# unprefixed `line` or `rc` would be clobbered by an ordinary-looking cell and
# break the protocol from the inside.
_mv_scratch=$(mktemp -d)
_mv_raw="${_mv_scratch}/cell.raw"
_mv_capped="${_mv_scratch}/cell.out"
trap 'rm -rf "${_mv_scratch}"' EXIT

# A runaway loop must not return a response too large to carry back. Raised
# from 16000 because cells now do real work: a `dnf install` transaction
# summary alone runs a few kilobytes, and truncation keeps the HEAD of the
# output -- so an over-cap cell loses its tail, which is exactly where a
# package manager puts the error it failed with.
_mv_output_limit=64000

# ------------------------------------------------------------------------------
# Millisecond clock — sets _mv_ms
# ------------------------------------------------------------------------------
# Sets a global rather than echoing, because $(...) forks a subshell. That is
# the trap this whole file is arranged to avoid; see the eval below.
# 10# forces base 10: microseconds like 012345 would otherwise parse as octal.
_mv_now_ms() {
  local _t=${EPOCHREALTIME/,/.}
  _mv_ms=$(( ${_t%.*} * 1000 + 10#${_t#*.} / 1000 ))
}

# ------------------------------------------------------------------------------
# `exit` guard
# ------------------------------------------------------------------------------
# Commands run in THIS shell, so a bare `exit` -- which models write out of
# habit at the end of a script -- would end the session and everything in it.
# A function shadows the builtin at the top level: `exit 3` fails the command
# with status 3 and the shell carries on. Scripts run as `bash script.sh` are
# separate processes with the real builtin, so they are unaffected. The
# protocol loop below never calls exit; it ends when stdin closes.
# ------------------------------------------------------------------------------
exit() {
  echo "[exit ${1:-0} ignored: this shell is the persistent session]" >&2
  return "${1:-0}"
}
logout() { exit "$@"; }

# ------------------------------------------------------------------------------
# Readiness handshake
# ------------------------------------------------------------------------------
# Anything set before this line becomes part of the image snapshot, because the
# supervisor blocks on it and only then does /ready pass. Never put identity
# here -- it would be cloned into every VM launched from the image.
# ------------------------------------------------------------------------------
jq -nc --argjson pid "$$" '{ready: true, pid: $pid}'

# ------------------------------------------------------------------------------
# Cell loop — one JSON object per line in each direction
# ------------------------------------------------------------------------------
while IFS= read -r _mv_line; do
  if ! _mv_code=$(jq -re '.code' <<<"${_mv_line}" 2>/dev/null); then
    # Answer anyway. A silent iteration would hang the server waiting on a
    # response that never arrives.
    jq -nc '{ok: false, stdout: "Malformed request line."}'
    continue
  fi

  _mv_now_ms; _mv_started=${_mv_ms}

  # The single most important line in this file. `out=$(eval ...)` would run the
  # cell in a subshell, so `balance=41` would vanish the moment it returned and
  # the entire demonstration would silently persist nothing. Redirecting to a
  # file keeps eval in THIS shell, which is what makes state survive.
  #
  # </dev/null matters just as much: stdin is the protocol channel, so a cell
  # calling `read` or `cat` would otherwise swallow the next request line.
  eval "${_mv_code}" >"${_mv_raw}" 2>&1 </dev/null
  _mv_rc=$?

  _mv_now_ms; _mv_elapsed=$((_mv_ms - _mv_started))

  # NULs cannot appear in JSON and would abort the encoder mid-response.
  head -c "${_mv_output_limit}" "${_mv_raw}" | tr -d '\000' > "${_mv_capped}"

  if [[ ${_mv_rc} -eq 0 ]]; then _mv_ok=true; else _mv_ok=false; fi

  jq -nc --rawfile stdout "${_mv_capped}" --argjson ok "${_mv_ok}" \
    --argjson ms "${_mv_elapsed}" \
    '{ok: $ok, stdout: $stdout, execution_ms: $ms}' \
    || jq -nc '{ok: false, stdout: "Cell output could not be encoded as JSON."}'
done

# Reaching here means stdin closed: the supervisor is gone, so the shell should
# be too. A cell that gets past the exit guard (`builtin exit`, `exec`, a
# `set -e` failure) also lands here; the supervisor restarts the session on
# its next command and says what was lost.
