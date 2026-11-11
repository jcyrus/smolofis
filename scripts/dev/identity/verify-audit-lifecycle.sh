#!/usr/bin/env bash
# Test-only guest fixture for installed-image A/B persistence acceptance.
# Synthetic enrollment stays locked and has no directory configuration.
set -euo pipefail
[[ ${EUID} -eq 0 ]] || { echo 'root required' >&2; exit 1; }
virtualization="$(systemd-detect-virt --vm || true)"
case "$virtualization" in
    qemu|kvm) ;;
    *) echo "QEMU/KVM test guest required (detected: ${virtualization:-unknown})" >&2; exit 1 ;;
esac
readonly BASE=/var/lib/smolofis/local-audit
readonly STATE=/etc/smolofis/panel-enrollment.json
readonly EVIDENCE=/var/lib/smolofis/ci-audit-lifecycle
readonly JOURNAL=/var/lib/smolofis/operations

job_phase() { sed -n 's/.*"phase": *"\([^" ]*\)".*/\1/p' | tail -n 1; }

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
    # The previous nightly can prepare/check byte persistence but cannot decode
    # the new event. Candidate-image verification must exercise the new reader.
    if [[ "${1:-full}" != baseline ]]; then
        local checkpoint listing
        checkpoint="$EVIDENCE/receipt-checkpoint.jsonl"
        [[ "$(stat -c '%a:%u:%h' "$checkpoint")" == 400:0:1 ]]
        historical="$(SMOLOFIS_LOCAL_AUDIT_FILE="$checkpoint" smolofis-admin console-operation-status "$(cat "$EVIDENCE/operation-id")")"
        grep -q '"source": "audit_history"' <<<"$historical"
        grep -q '"outcome": "applied"' <<<"$historical"
        listing="$(SMOLOFIS_LOCAL_AUDIT_FILE="$checkpoint" smolofis-admin console-operation-list)"
        grep -Fq "$(cat "$EVIDENCE/operation-id")" <<<"$listing"
        grep -q '"outcome": "applied"' <<<"$listing"
        [[ "$(stat -c '%a:%u' "$JOURNAL")" == 700:0 ]]
        [[ "$(stat -c '%a:%u:%h' "$JOURNAL/journal.json")" == 600:0:1 ]]
        [[ "$(stat -c '%a:%u:%h' "$JOURNAL/journal.lock")" == 600:0:1 ]]
        sha256sum --check --status "$EVIDENCE/journal-hashes"
        status="$(smolofis-admin console-job-status "$(cat "$EVIDENCE/completed-job-id")")"
        [[ "$(job_phase <<<"$status")" == succeeded ]]
        grep -q '"outcome": "identity_verified"' <<<"$status"
        status="$(smolofis-admin console-job-status "$(cat "$EVIDENCE/interrupted-job-id")")"
        if [[ -e "$EVIDENCE/journal-recovered" ]]; then
            [[ "$(job_phase <<<"$status")" == unknown ]]
            grep -q '"outcome": "interrupted"' <<<"$status"
            status="$(smolofis-admin console-job-status "$(cat "$EVIDENCE/executed-job-id")")"
            [[ "$(job_phase <<<"$status")" == succeeded ]]
            grep -q '"outcome": "identity_verified"' <<<"$status"
        else
            [[ "$(job_phase <<<"$status")" == running ]]
        fi
        listing="$(smolofis-admin console-job-list)"
        grep -Fq "$(cat "$EVIDENCE/completed-job-id")" <<<"$listing"
        grep -Fq "$(cat "$EVIDENCE/interrupted-job-id")" <<<"$listing"
        echo 'PASS: durable operation journal and typed results preserved'
        echo 'PASS: protected checkpoint-only history is readable by the candidate'
    fi
    echo 'PASS: locked enrollment receipt, live log and protected archive preserved'
}

case "${1:-}" in
    prepare)
        # Never replace actual enrollment or opt an existing audit store in.
        [[ ! -e "$BASE" && ! -L "$BASE" && ! -e "$STATE" && ! -L "$STATE" && ! -e "$EVIDENCE" && ! -e "$JOURNAL" && ! -L "$JOURNAL" ]]
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
        # Synthetic test-only history, prepared even on an older nightly. Native
        # tests exercise real checkpoint emission; this fixture tests the new
        # reader across installed-image updates/fallback without enabling access.
        checkpoint="$EVIDENCE/receipt-checkpoint.jsonl"
        printf '{"version":1,"timestamp_ms":0,"event":"local_receipt_preserved","actor_guid":null,"actor_uid":0,"operation_id":"%s","operation":"lockdown","outcome":"applied"}\n' "$operation" > "$checkpoint"
        chmod 0400 "$checkpoint"
        # Previous images can prepare the new format without executing new CLI
        # commands. Native tests cover genuine journal writes and crash recovery.
        install -d -m 0700 "$JOURNAL"
        completed="$(printf '%064d' 7)"
        interrupted="$(printf '%064d' 8)"
        (
            umask 077
            set -o noclobber
            printf '{"version":1,"machine_id_digest":"%s","operations":[{"id":"%s","kind":"machine_identity_check","actor_uid":0,"transitions":[{"phase":"queued","timestamp_ms":0},{"phase":"running","timestamp_ms":1},{"phase":"succeeded","timestamp_ms":2}],"outcome":"identity_verified"},{"id":"%s","kind":"machine_identity_check","actor_uid":0,"transitions":[{"phase":"queued","timestamp_ms":0},{"phase":"running","timestamp_ms":1}],"outcome":null}]}\n' "$machine" "$completed" "$interrupted" > "$JOURNAL/journal.json"
            : > "$JOURNAL/journal.lock"
        )
        printf '%s\n' "$completed" > "$EVIDENCE/completed-job-id"
        printf '%s\n' "$interrupted" > "$EVIDENCE/interrupted-job-id"
        sha256sum "$JOURNAL/journal.json" > "$EVIDENCE/journal-hashes"
        sha256sum /etc/machine-id /etc/smolofis/identity/machine-id "$STATE" "$BASE/events.jsonl" "$archive" "$manifest" "$checkpoint" "$JOURNAL/journal.lock" > "$EVIDENCE/hashes"
        sync
        verify baseline
        ;;
    recover-journal)
        verify
        [[ ! -e "$EVIDENCE/journal-recovered" ]]
        completed_before="$(smolofis-admin console-job-status "$(cat "$EVIDENCE/completed-job-id")")"
        recovered="$(smolofis-admin console-job-recover)"
        grep -q '"recovered":1' <<<"$recovered"
        [[ "$(smolofis-admin console-job-status "$(cat "$EVIDENCE/completed-job-id")")" == "$completed_before" ]]
        smolofis-admin console-job-run > "$EVIDENCE/executed-job.json"
        sed -n 's/.*"id": *"\([0-9a-f]\{64\}\)".*/\1/p' "$EVIDENCE/executed-job.json" > "$EVIDENCE/executed-job-id"
        [[ "$(cat "$EVIDENCE/executed-job-id")" =~ ^[0-9a-f]{64}$ ]]
        [[ "$(smolofis-admin console-job-status "$(cat "$EVIDENCE/completed-job-id")")" == "$completed_before" ]]
        sha256sum --check --status "$EVIDENCE/hashes"
        sha256sum "$JOURNAL/journal.json" > "$EVIDENCE/journal-hashes"
        touch "$EVIDENCE/journal-recovered"
        sync
        verify
        echo 'PASS: interrupted journal recovered and fresh identity operation completed'
        ;;
    verify) verify ;;
    *) echo 'usage: verify-audit-lifecycle.sh prepare|verify|recover-journal' >&2; exit 2 ;;
esac
