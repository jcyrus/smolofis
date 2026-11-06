"""LAB ONLY: expire remembered logins with Gitea stopped and sessions in memory.

The runner checks Gitea 28.2.0 before stopping it. This adapter only understands
that release's SQLite layout. It is not a production administration endpoint.
"""
import configparser
import json
import os
from pathlib import Path
import shutil
import sqlite3


def refuse(message):
    raise SystemExit('REFUSED: ' + message)


root = Path('/gitea/gitea')
config = configparser.ConfigParser(interpolation=None, strict=False)
try:
    # Gitea permits root keys before its first INI section.
    config.read_string('[DEFAULT]\n' + (root / 'conf/app.ini').read_text())
except (OSError, configparser.Error):
    refuse('could not parse the pinned Gitea configuration')
if config.get('database', 'DB_TYPE', fallback='') != 'sqlite3':
    refuse('unsupported database engine')
if config.get('database', 'PATH', fallback='') != '/data/gitea/gitea.db':
    refuse('unexpected database path')
if config.get('session', 'PROVIDER', fallback='') != 'memory':
    refuse('browser-session provider must be memory')
database = root / 'gitea.db'
if database.is_symlink() or not database.is_file():
    refuse('database must be an existing regular file')
fixture = json.loads(Path('/evidence/session-fixture.json').read_text())
pending = fixture['report']
with sqlite3.connect(f'file:{database}?mode=rw', uri=True, timeout=5) as db:
    db.execute('BEGIN EXCLUSIVE')
    required = {'id', 'user_id', 'token_hash', 'expires_unix'}
    columns = {row[1] for row in db.execute('PRAGMA table_info(auth_token)')}
    if not required <= columns:
        refuse('remembered-login schema differs from the pinned release')
    user = db.execute('SELECT id, name, prohibit_login, login_source FROM user WHERE id = ?',
                      (pending['account_id'],)).fetchone()
    if user != (pending['account_id'], pending['username'], 1, pending['source_id']):
        refuse('target account is not the expected prohibited directory user')
    if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
        refuse('database integrity check failed')
    removed = db.execute('DELETE FROM auth_token').rowcount
    if removed < 1:
        refuse('legacy remembered-login fixture was not present')
    # Old file-provider sessions must not revive if the provider changes later.
    sessions = root / 'sessions'
    if sessions.is_symlink():
        refuse('unexpected symlink at the old session directory')
    if sessions.exists():
        metadata = sessions.stat()
        shutil.rmtree(sessions)
        sessions.mkdir(mode=metadata.st_mode & 0o777)
        os.chown(sessions, metadata.st_uid, metadata.st_gid)
    db.commit()
Path('/evidence/session-maintenance.json').write_text(json.dumps({
    'scope': 'all-gitea-browser-users', 'remember_tokens_removed': removed,
    'target_account_id': pending['account_id'], 'gitea_version': '28.2.0',
}) + '\n')
print('PASS: offline remembered-login revocation; memory sessions end with the stopped process', flush=True)
