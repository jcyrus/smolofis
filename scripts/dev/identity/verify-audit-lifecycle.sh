#!/usr/bin/env bash
# Test-only guest fixture for installed-image A/B persistence acceptance.
# Synthetic enrollment stays locked and has no directory configuration.
set -euo pipefail
[[ ${EUID} -eq 0 ]] || { echo 'root required' >&2; exit 1; }
[[ "$(systemd-detect-virt --vm)" == qemu ]] || { echo 'QEMU test guest required' >&2; exit 1; }
readonly BASE=/var/lib/smolofis/local-audit
readonly STATE=/etc/smolofis/panel-enrollment.json
readonly EVIDENCE=/var/lib/smolofis/ci-audit-lifecycle

verify() {
    [[ -d "$EVIDENCE" && ! -L "$EVIDENCE" ]]
    sha256sum --check --status "$EVIDENCE/hashes"
    [[ "$(stat -c '%a:%u:%h' "$STATE")" == 644:0:1 ]]
    [[ "$(stat -c '%a:%u' "$BASE")" == 700:0 ]]
    [[ "$(stat -c '%a:%u' "$BASE/archives")" == 700:0 ]]
    [[ "$(stat -c '%a:%u:%h' "$BASE/events.jsonl")" == 600:0:1 ]]
    local archive status historical
    archive="$(cat "$EVIDENCE/archive-path")"
    [[ "$(stat -c '%a:%u:%h' "$archive")" == 400:0:1 ]]
    status="$(smolofis-admin console-status)"
    grep -q '"locked": true' <<<"$status"
    grep -q '"version": 2' <<<"$status"
    historical="$(SMOLOFIS_LOCAL_AUDIT_FILE="$archive" smolofis-admin console-operation-status "$(cat "$EVIDENCE/operation-id")")"
    grep -q '"source": "audit_history"' <<<"$historical"
    grep -q '"outcome": "applied"' <<<"$historical"
    grep -q '"directory_checked": false' <<<"$historical"
    echo 'PASS: locked enrollment receipt, live log and protected archive preserved'
}

case "${1:-}" in
    prepare)
        # Never replace actual enrollment or opt an existing audit store in.
        [[ ! -e "$BASE" && ! -L "$BASE" && ! -e "$STATE" && ! -L "$STATE" && ! -e "$EVIDENCE" ]]
        install -d -m 0700 "$EVIDENCE"
        smolofis-admin console-audit-init >/dev/null
        machine="$(tr -d '\n' < /etc/machine-id | sha256sum | cut -d' ' -f1)"
        (
            umask 022
            set -o noclobber
            printf '{"version":1,"device_id":"%064d","machine_id_digest":"%s","generation":"%064d","config_digest":"%064d","first_admin_guid":"%032d","locked":true}\n' \
                1 "$machine" 2 3 4 > "$STATE"
        )
        operation="$(smolofis-admin console-lock | awk '$1 == "Operation" {print $2}')"
        [[ "$operation" =~ ^[0-9a-f]{64}$ ]]
        # Supersede the first receipt so lookup must use the archive.
        smolofis-admin console-lock >/dev/null
        smolofis-admin console-audit-archive > "$EVIDENCE/archive-result.json"
        archives=("$BASE"/archives/*.jsonl)
        [[ ${#archives[@]} -eq 1 && -f "${archives[0]}" ]]
        archive="${archives[0]}"
        manifest="${archive%.jsonl}.manifest.json"
        digest="$(sed -n 's/.*"sha256": *"\([0-9a-f]*\)".*/\1/p' "$manifest")"
        [[ "$digest" == "$(sha256sum "$archive" | cut -d' ' -f1)" ]]
        printf '%s\n' "$archive" > "$EVIDENCE/archive-path"
        printf '%s\n' "$operation" > "$EVIDENCE/operation-id"
        sha256sum "$STATE" "$BASE/events.jsonl" "$archive" "$manifest" > "$EVIDENCE/hashes"
        verify
        ;;
    verify) verify ;;
    *) echo 'usage: verify-audit-lifecycle.sh prepare|verify' >&2; exit 2 ;;
esac
