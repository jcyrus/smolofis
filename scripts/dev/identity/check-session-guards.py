"""Run with Gitea stopped; prove refusal leaves authentication state unchanged."""
import configparser
import io
import json
from pathlib import Path
import runpy
import sqlite3

config_file = Path('/gitea/gitea/conf/app.ini')
fixture_file = Path('/evidence/session-fixture.json')
original_config = config_file.read_bytes()
original_fixture = fixture_file.read_bytes()


def counts():
    with sqlite3.connect('file:/gitea/gitea/gitea.db?mode=ro', uri=True) as db:
        return tuple(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                     for table in ('user', 'repository', 'access_token', 'auth_token'))


before = counts()
try:
    fixture = json.loads(original_fixture)
    fixture['report']['account_id'] = -1
    fixture_file.write_text(json.dumps(fixture))
    try:
        runpy.run_path('/usr/local/bin/lab-revoke-sessions', run_name='__main__')
    except SystemExit as error:
        if not str(error).startswith('REFUSED: target account'):
            raise
    else:
        raise SystemExit('FAIL: maintenance accepted a mismatched target')
    if counts() != before:
        raise SystemExit('FAIL: target refusal changed database rows')
    print('PASS: mismatched account is refused without changing authentication data', flush=True)

    fixture_file.write_bytes(original_fixture)
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.read_string('[DEFAULT]\n' + original_config.decode())
    config.set('database', 'DB_TYPE', 'unsupported-test-engine')
    text = io.StringIO()
    config.write(text)
    config_file.write_text(text.getvalue())
    try:
        runpy.run_path('/usr/local/bin/lab-revoke-sessions', run_name='__main__')
    except SystemExit as error:
        if not str(error).startswith('REFUSED: unsupported database'):
            raise
    else:
        raise SystemExit('FAIL: maintenance accepted an unsupported database')
    if counts() != before:
        raise SystemExit('FAIL: engine refusal changed database rows')
    print('PASS: unsupported database is refused without changing authentication data', flush=True)
finally:
    config_file.write_bytes(original_config)
    fixture_file.write_bytes(original_fixture)
