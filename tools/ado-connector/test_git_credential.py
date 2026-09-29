import json
import runpy
from pathlib import Path

import pytest


def api():
    p = Path(__file__).with_name('git_credential.py')
    assert p.exists(), 'scoped credential helper is missing'
    return runpy.run_path(str(p))


@pytest.mark.parametrize('protocol,host,path', [
    ('https','evil.example','org/project/_git/repo'),
    ('http','dev.azure.com','org/project/_git/repo'),
    ('https','dev.azure.com','org/other/_git/repo'),
    ('https','user@dev.azure.com','org/project/_git/repo'),
    ('https','dev.azure.com','org/project/_git/../repo'),
    ('https','dev.azure.com','org/project/_git/%2e%2e/repo'),
])
def test_refuses_other_targets(tmp_path, protocol,host,path):
    f = api()['credentials_for_request']
    assert f({'protocol':protocol,'host':host,'path':path}, tmp_path/'missing.json') == {}


def test_exact_scope_and_private_files(tmp_path):
    f = api()['credentials_for_request']
    secret=tmp_path/'key';secret.write_text('synthetic-value');secret.chmod(0o600)
    config=tmp_path/'config.json';config.write_text(json.dumps({'repositories': [
        {'url':'https://dev.azure.com/org/project/_git/repo','credential_file':str(secret)}]}));config.chmod(0o600)
    request={'protocol':'https','host':'dev.azure.com','path':'org/project/_git/repo'}
    assert f(request,config) == {'username':'pursers','password':'synthetic-value'}
    secret.chmod(0o644)
    assert f(request,config) == {}
    secret.chmod(0o600)
    link=tmp_path/'link';link.symlink_to(secret)
    config.write_text(json.dumps({'repositories':[{'url':'https://dev.azure.com/org/project/_git/repo','credential_file':str(link)}]}))
    assert f(request,config) == {}


def test_git_protocol_only_returns_credentials_to_a_pipe(tmp_path):
    import subprocess
    import sys
    secret = tmp_path / 'key'
    secret.write_text('synthetic-value')
    secret.chmod(0o600)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'repositories': [{
        'url': 'https://dev.azure.com/org/project/_git/repo',
        'credential_file': str(secret)}]}))
    config.chmod(0o600)
    command = [sys.executable, str(Path(__file__).with_name('git_credential.py')),
               '--config', str(config), 'get']
    request = 'protocol=https\nhost=dev.azure.com\npath=org/project/_git/repo\n\n'
    result = subprocess.run(command, input=request, capture_output=True, text=True, check=True)
    assert result.stdout == 'username=pursers\npassword=synthetic-value\n\n'
    assert result.stderr == ''
    with (tmp_path / 'service.log').open('w+') as stream:
        result = subprocess.run(command, input=request, stdout=stream,
                                stderr=subprocess.PIPE, text=True, check=True)
        stream.seek(0)
        assert stream.read() == ''
    assert result.stderr == ''
