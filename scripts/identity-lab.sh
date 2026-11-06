#!/usr/bin/env bash
# Run only synthetic identities on an internal Docker network.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/dev/identity/compose.yml"
compose() { docker compose -f "$COMPOSE_FILE" "$@"; }
control() { compose exec -T dc python3 /usr/local/bin/lab-control "$@"; }

trust_ca() {
    compose exec -T --user root gitea sh -ec '
        cp /lab/ca.crt /usr/local/share/ca-certificates/smolofis-lab.crt
        update-ca-certificates >/dev/null
    '
}

prepare() {
    compose up --build --detach --wait dc files gitea
    # Install the disposable CA; never disable LDAP certificate verification.
    trust_ca
    # Restart after configuration so the web process reloads its auth sources.
    # Expand the password command inside the container, never on the host.
    # shellcheck disable=SC2016
    compose exec -T --user git gitea sh -ec '
        if ! gitea admin auth list | grep -q "SmolOfis-Lab"; then
            gitea admin auth add-ldap --name SmolOfis-Lab \
                --security-protocol ldaps --host dc --port 636 \
                --bind-dn "CN=ldap-reader,CN=Users,DC=lab,DC=smolofis,DC=test" \
                --bind-password "$(cat /lab/ldap-bind-password)" \
                --user-search-base "CN=Users,DC=lab,DC=smolofis,DC=test" \
                --user-filter "(&(objectClass=user)(sAMAccountName=%s)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))" \
                --username-attribute sAMAccountName --email-attribute mail
        fi
    '
    compose restart gitea
    # The synthetic admin password is read inside the container.
    # shellcheck disable=SC2016
    compose exec -T --user git gitea sh -ec '
        if ! gitea admin user list | grep -qw lab-admin; then
            gitea admin user create --username lab-admin \
                --password "$(cat /lab/gitea-admin-password)" \
                --email lab-admin@lab.smolofis.test --admin --must-change-password=false
        fi
    '
}

case "${1:-test}" in
panel-auth|panel-enrollment|panel-private-https)
    [[ -n "${2:-}" && -f "$2" ]] || { echo 'panel-auth requires an absolute Linux panel binary path' >&2; exit 2; }
    case "$2" in /*) ;; *) echo 'panel binary path must be absolute' >&2; exit 2 ;; esac
    export SMOLOFIS_LAB_PANEL_BINARY="$2"
    compose up --build --detach --wait dc
    compose exec -T dc python3 /usr/local/bin/lab-panel-auth prepare
    mode=verify
    [[ "$1" != panel-enrollment ]] || mode=enrollment
    [[ "$1" != panel-private-https ]] || mode=private-https
    docker compose -f "$COMPOSE_FILE" -f "${SCRIPT_DIR}/dev/identity/compose.panel-auth.yml" run --rm panel-auth "$mode"
    ;;
up)
    compose up --build --detach --wait dc files gitea
    ;;
test)
    prepare
    compose run --rm verify
    control snapshot
    control rotate
    compose run --rm verify
    # Always restore Alice if an assertion fails, keeping reruns usable.
    trap 'control enable >/dev/null' EXIT
    control disable
    compose run --rm verify verify --disabled
    control enable
    trap - EXIT
    compose restart dc files gitea
    compose up --detach --wait dc files gitea
    control compare
    compose run --rm verify
    ;;
migration)
    prepare
    compose run --rm verify
    compose run --rm verify migration
    ;;
sessions)
    # Start with a real legacy remembered login, then exercise the v1 policy.
    export SMOLOFIS_LAB_REMEMBER_DAYS=31
    prepare
    compose run --rm verify
    compose run --rm verify migration --prepare-session-revocation
    gitea_version="$(compose exec -T --user git gitea gitea --version)"
    case "$gitea_version" in
        'gitea version 28.2.0 '*) ;;
        *) echo 'refusing session maintenance: untested Gitea version' >&2; exit 1 ;;
    esac
    export SMOLOFIS_LAB_REMEMBER_DAYS=0
    compose stop gitea
    trap 'compose up --detach --wait gitea >/dev/null' EXIT
    if [[ -n "$(compose ps --status running --quiet gitea)" ]]; then
        echo 'refusing session maintenance: Gitea is still running' >&2
        exit 1
    fi
    compose run --rm --no-deps --entrypoint python3 maintenance /usr/local/bin/lab-check-session-guards
    compose run --rm --no-deps maintenance
    compose up --detach --wait gitea
    trust_ca
    compose restart gitea
    compose up --detach --wait gitea
    trap - EXIT
    compose run --rm verify migration --check-session-revocation
    compose run --rm verify
    ;;
live-smb)
    prepare
    compose run --rm verify
    python3 "${SCRIPT_DIR}/dev/identity/live-smb.py" host
    compose run --rm verify
    ;;
backup)
    prepare
    compose run --rm verify
    compose run --rm verify migration
    control snapshot
    compose exec -T files python3 /usr/local/bin/lab-storage snapshot
    # A same-DC cold snapshot: all writers stop before any volume is archived.
    compose stop
    if [[ -n "$(compose ps --status running --quiet)" ]]; then
        echo 'refusing snapshot: lab still has running containers' >&2
        exit 1
    fi
    restore_project="smolofis-identity-restore-$(date +%s)-${RANDOM}"
    backup_volume="${restore_project}_backup"
    docker volume create --label smolofis.identity-lab=backup "$backup_volume" >/dev/null
    lab_volumes=(directory directory-config credentials member member-config shares git evidence)
    for volume in "${lab_volumes[@]}"; do
        source_volume="smolofis-identity-lab_${volume}"
        target_volume="${restore_project}_${volume}"
        if docker volume inspect "$target_volume" >/dev/null 2>&1; then
            echo 'refusing to overwrite an existing restore volume' >&2
            exit 1
        fi
        docker volume inspect "$source_volume" >/dev/null
        docker volume create --label "com.docker.compose.project=${restore_project}" \
            --label "com.docker.compose.volume=${volume}" "$target_volume" >/dev/null
        docker run --rm --network none --entrypoint sh \
            --mount "type=volume,src=${source_volume},dst=/source,readonly" \
            --mount "type=volume,src=${backup_volume},dst=/backup" \
            smolofis-identity-lab:local -ec \
            'umask 077; tar --acls --xattrs --xattrs-include="*" --numeric-owner -cpf "/backup/$1.tar" -C /source .' \
            sh "$volume"
        docker run --rm --network none --entrypoint sh \
            --mount "type=volume,src=${target_volume},dst=/target" \
            --mount "type=volume,src=${backup_volume},dst=/backup,readonly" \
            smolofis-identity-lab:local -ec \
            'tar --acls --xattrs --xattrs-include="*" --numeric-owner --same-owner -xpf "/backup/$1.tar" -C /target' \
            sh "$volume"
    done
    # Release the original lab's network; retain its original volumes untouched.
    compose down
    restored() { docker compose -p "$restore_project" -f "$COMPOSE_FILE" "$@"; }
    trap 'restored down >/dev/null' EXIT
    restored up --detach --wait dc files gitea
    restored exec -T --user root gitea sh -ec '
        cp /lab/ca.crt /usr/local/share/ca-certificates/smolofis-lab.crt
        update-ca-certificates >/dev/null
    '
    restored restart gitea
    restored exec -T dc python3 /usr/local/bin/lab-control compare
    restored exec -T files python3 /usr/local/bin/lab-storage compare
    restored run --rm verify
    restored run --rm verify migration --check-restored
    restored down
    trap - EXIT
    echo "PASS: cold restore into fresh volumes (${restore_project}); original volumes retained"
    ;;
stop)
    compose stop
    ;;
status)
    compose ps
    ;;
*) echo 'usage: scripts/identity-lab.sh [up|test|migration|sessions|live-smb|backup|stop|status|panel-auth /absolute/linux-binary|panel-enrollment /absolute/linux-binary|panel-private-https /absolute/linux-binary]' >&2; exit 2 ;;
esac
