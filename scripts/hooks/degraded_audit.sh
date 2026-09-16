#!/usr/bin/env bash
# Best-effort durable records for hook paths that cannot evaluate their guard.
# This writer deliberately has no Python or jq dependency.

_source="${1:-}"
_verdict="${2:-}"
_reason="${3:-}"
_operation="${4:-}"
[ "$#" -ge 3 ] || exit 0
[ -n "${HOME:-}" ] || exit 0

_store="${GENESIS_DEGRADED_AUDIT_DIR:-$HOME/.genesis/hook_degradations}"
case "$_store" in
    /*) ;;
    *) _store="$HOME/.genesis/hook_degradations" ;;
esac

# Escape the small JSON string fields without requiring jq or Python. Non-ASCII
# bytes remain UTF-8 JSON; control bytes and JSON metacharacters are escaped.
_escape_json() {
    local value="$1" out="" char code hex i
    local LC_ALL=C
    for ((i = 0; i < ${#value}; i++)); do
        char="${value:i:1}"
        case "$char" in
            '"') out+='\"' ;;
            \\) out+='\\' ;;
            $'\b') out+='\b' ;;
            $'\f') out+='\f' ;;
            $'\n') out+='\n' ;;
            $'\r') out+='\r' ;;
            *)
                printf -v code '%d' "'${char}" 2>/dev/null || code=32
                if [ "$code" -lt 32 ]; then
                    printf -v hex '%04x' "$code"
                    out+="\\u$hex"
                else
                    out+="$char"
                fi
                ;;
        esac
    done
    REPLY="$out"
}

_escape_json "$_source"; source_q="$REPLY"
_escape_json "$_verdict"; verdict_q="$REPLY"
_escape_json "$_reason"; reason_q="$REPLY"
_escape_json "$_operation"; operation_q="$REPLY"

stamp="$(date -u +%Y%m%dT%H%M%S_%6N 2>/dev/null || true)"
if [[ ! "$stamp" =~ ^[0-9]{8}T[0-9]{6}_[0-9]{6}$ ]]; then
    printf -v stamp '%(%Y%m%dT%H%M%S)T_000000' -1 2>/dev/null || exit 0
fi

payload="{\"timestamp\":\"${stamp}Z\",\"event\":\"hook_degraded\",\"source\":\"${source_q}\",\"verdict\":\"${verdict_q}\",\"reason\":\"${reason_q}\""
if [ -n "$_operation" ]; then
    payload+=",\"operation\":\"${operation_q}\""
fi
payload+='}'

umask 077
mkdir -p -m 700 "$_store" 2>/dev/null || exit 0
chmod 700 "$_store" 2>/dev/null || true

pid="${BASHPID:-$$}"
for ((n = 0; n < 100; n++)); do
    stem="${stamp}Z-${pid}-${n}"
    path="$_store/$stem.jsonl"
    staging="$_store/$stem.part"
    # noclobber makes the staging create exclusive. Never remove a staging path
    # unless this invocation successfully created it.
    if ! (set -C; printf '%s\n' "$payload" > "$staging"); then
        continue
    fi
    chmod 600 "$staging" 2>/dev/null || true
    # Hard-link publication fails on a taken name instead of replacing it.
    if ln "$staging" "$path" 2>/dev/null; then
        rm -f -- "$staging" 2>/dev/null || true
        exit 0
    fi
    rm -f -- "$staging" 2>/dev/null || true
done

exit 0
