import io
import json
import os
import runpy
import stat
import subprocess
from pathlib import Path

import pytest


HELPER = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'event_diagnostics.py'))
RUNNER = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'event_seat.py'))


def test_redacts_runtime_secrets_headers_and_jwts():
    secret = 'runtime-secret-assembled-here'
    text = f'failed {secret} Authorization: Bearer example_bearer_token token={secret}'
    clean = HELPER['redact'](text, [secret])
    assert secret not in clean
    assert 'example_bearer_token' not in clean
    assert '[REDACTED]' in clean


def test_evidence_is_owner_private_bounded_correlated_and_retained(tmp_path):
    state = tmp_path / 'state.json'
    directory = tmp_path / 'diagnostics'
    secret = 'synthetic-secret-built-at-runtime'
    path = HELPER['write_evidence'](state, 'attempt-1', {
        'operation_id': 'attempt-1', 'board_id': 'pursers', 'ticket_id': 'TK-example',
        'attempt_id': 'attempt-1', 'outcome': 'model_nonzero_exit',
    }, f'provider failed with {secret}', [secret])
    info = path.lstat()
    record = json.loads(path.read_text())
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert record['ticket_id'] == 'TK-example'
    assert record['operation_id'] == record['attempt_id'] == 'attempt-1'
    assert secret not in path.read_text()
    assert record['omissions'] == {
        'stdout': 'not_captured', 'private_prompt': 'not_captured', 'stderr_truncated': False}
    assert path.stat().st_size <= HELPER['MAX_FILE_BYTES']


def test_stderr_drain_keeps_bounded_tail_without_blocking_pipe_reads():
    capture = HELPER['BoundedStderr'](limit=8)

    class Chunks:
        def __init__(self): self.values = iter((b'abcdef', b'ghijkl'))
        def read1(self, _size): return next(self.values, b'')
        def close(self): pass

    capture.drain(Chunks())
    assert capture.text() == 'efghijkl'
    assert capture.total_bytes == 12
    assert capture.truncated is True


def test_diagnostic_rejects_symlinked_directory_and_file(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir(mode=0o700)
    link = tmp_path / 'diagnostics'
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='directory'):
        HELPER['write_evidence'](tmp_path / 'state.json', 'a', {}, 'failure')

    link.unlink()
    directory = tmp_path / 'diagnostics'
    directory.mkdir(mode=0o700)
    target = directory / 'diag-b.json'
    target.symlink_to(outside / 'secret')
    with pytest.raises(ValueError, match='untrusted'):
        HELPER['write_evidence'](tmp_path / 'state.json', 'b', {}, 'failure')


def test_subprocess_fake_retains_redacted_nonzero_stderr_without_stdout(tmp_path, monkeypatch):
    module = RUNNER
    cfg = {'seat_id': 'worker-a', 'role': 'worker', 'provider': 'fake', 'model': 'fake',
           'seat_dir': str(tmp_path), 'board_script': str(tmp_path / 'board.sh'),
           'state_file': str(tmp_path / 'state.json'), 'token_file': str(tmp_path / 'token'),
           'goose': '/fake/goose', 'mcp': '/fake/mcp', 'central_url': 'http://localhost/mcp',
           'home_board': 'home', 'repository_root': str(tmp_path / 'clones'),
           'max_runs_per_hour': None, 'turn_timeout_s': 10}
    secret = 'synthetic-child-secret-built-in-test'
    cfg['token_file'] = str(tmp_path / 'token')
    Path(cfg['token_file']).write_text(secret)
    Path(cfg['token_file']).chmod(0o600)
    runner = module['EventSeatRunner'](cfg)
    runner.active_boards = ['home']

    async def refresh():
        return None
    runner.refresh_presence = refresh
    runner._active_turn = {'board': 'home', 'ticket': 'TK-fake', 'attempt_id': 'attempt-fake',
                           'claim_ref': 'claim-fake'}
    stderr = b'Provider unavailable; request failed Authorization: Bearer example_bearer_token ' + secret.encode()

    class Process:
        def __init__(self):
            self.stderr = io.BytesIO(stderr)
        def wait(self, timeout=None):
            return 9
        def poll(self):
            return 9
        def terminate(self):
            pytest.fail('already exited child must not be terminated')
        def kill(self):
            pytest.fail('already exited child must not be killed')

    monkeypatch.setattr(module['subprocess'], 'Popen', lambda *a, **kw: Process())
    with pytest.raises(subprocess.CalledProcessError) as caught:
        runner._run(['fake-child'])
    assert caught.value.returncode == 9
    evidence = list((tmp_path / 'diagnostics').glob('diag-*.json'))
    assert len(evidence) == 1
    text = evidence[0].read_text()
    assert secret not in text and 'example_bearer_token' not in text
    assert 'Provider unavailable; request failed' in text
    assert runner._child_result['diagnostic']['ref'] == str(evidence[0])
    assert runner._child_result['outcome'] == 'model_process_failed'
    assert runner._child_result['diagnostic']['outcome'] == 'provider_unavailable'
    assert runner._child_result['diagnostic']['omissions']['stdout'] == 'not_captured'
    assert 'Provider unavailable; request failed' in text and '"stderr":' in text


@pytest.mark.parametrize(('status','owner','expected'), [
    ('submitted', False, 'ticket_submitted'),
    ('closed', False, 'ticket_closed'),
    ('needs_human', False, 'ticket_human_held'),
    ('claimed', True, 'unfinished_with_owned_lease'),
    ('claimed', False, 'lease_not_owned'),
])
def test_turn_outcome_requires_authoritative_ticket_and_exact_live_claim(tmp_path, status, owner, expected):
    from types import SimpleNamespace
    import asyncio
    runner=RUNNER['EventSeatRunner']({'seat_id':'worker-a','role':'worker',
        'provider':'fake','model':'fake','seat_dir':str(tmp_path),
        'board_script':str(tmp_path/'board.sh'),'state_file':str(tmp_path/'state.json'),
        'token_file':str(tmp_path/'token'),'goose':'/fake/goose','mcp':'/fake/mcp',
        'central_url':'http://localhost/mcp','home_board':'home',
        'repository_root':str(tmp_path/'clones'),'max_runs_per_hour':None})
    ticket={'ticket_id':'TK-state','status':status}
    if status=='claimed':
        ticket.update(claimed_by_agent_id='AI-own' if owner else 'AI-other',
            claimed_by_principal_id='PR-own' if owner else 'PR-other',
            lease_expires_at_epoch=__import__('time').time()+1000,claimed_at='claim-one')
    identity=SimpleNamespace(board_id='pursers',agent_name='worker-a',role='worker',
                             agent_id='AI-own',principal_id='PR-own')
    class Client:
        def __init__(self):self.identity=identity
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def ticket_get(self,*args,**kwargs):return {'ticket':ticket}
    runner.client=lambda board: Client()
    result=asyncio.run(runner.verify_turn_outcome({'board':'pursers','ticket':'TK-state',
        'claim_ref':json.dumps(['pursers','TK-state','worker','claim-one'],separators=(',',':'))
        if owner else 'old-claim'}))
    assert result['outcome']==expected


@pytest.mark.parametrize(('child_outcome','returncode','expected'), [
    ('child_exit_zero', 0, 'unfinished_with_owned_lease'),
    ('model_process_failed', 7, 'unfinished_with_owned_lease'),
    ('model_timeout', None, 'unfinished_with_owned_lease'),
])
def test_reviewer_submitted_ticket_requires_verdict_or_released_review_lease(
        tmp_path, child_outcome, returncode, expected):
    from types import SimpleNamespace
    import asyncio
    import time

    cfg={'seat_id':'reviewer-a','role':'reviewer','provider':'fake','model':'fake',
        'seat_dir':str(tmp_path),'board_script':str(tmp_path/'board.sh'),
        'state_file':str(tmp_path/'state.json'),'token_file':str(tmp_path/'token'),
        'goose':'/fake/goose','mcp':'/fake/mcp','central_url':'http://localhost/mcp',
        'home_board':'pursers','repository_root':str(tmp_path/'clones'),
        'max_runs_per_hour':None}
    runner=RUNNER['EventSeatRunner'](cfg)
    ticket={'ticket_id':'TK-review','status':'submitted','review_lease':{
        'reviewer_agent_id':'AI-reviewer','reviewer_principal_id':'PR-reviewer',
        'expires_at_epoch':time.time()+1000,'claimed_at':'review-claim'}}
    identity=SimpleNamespace(board_id='pursers',agent_name='reviewer-a',role='reviewer',
        agent_id='AI-reviewer',principal_id='PR-reviewer')
    class Client:
        def __init__(self):self.identity=identity
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def ticket_get(self,*args,**kwargs):return {'ticket':ticket}
    runner.client=lambda board:Client()
    claim_ref=json.dumps(['pursers','TK-review','reviewer','review-claim'],separators=(',',':'))
    turn={'board':'pursers','ticket':'TK-review','claim_ref':claim_ref}

    # Central still says submitted, but the exact live review lease means no
    # verdict was recorded. This remains incomplete for every child result.
    result=asyncio.run(runner.verify_turn_outcome(turn))
    assert result=={'outcome':expected,'ticket_status':'submitted','owned':True}
    last={}
    runner._apply_child_result(last,{'outcome':child_outcome,'error_class':None,
        'returncode':returncode,'diagnostic':None})
    assert last['child_outcome']==child_outcome
    assert last['child_returncode']==returncode
    assert last['outcome']==('model_nonzero_exit' if child_outcome=='model_process_failed'
                             else 'model_timeout' if child_outcome=='model_timeout'
                             else 'exited_pending_verification')

    # Once the review lease is gone, the authoritative submitted status means
    # the review claim is no longer live; do not continue the old attempt.
    ticket.pop('review_lease')
    result=asyncio.run(runner.verify_turn_outcome(turn))
    assert result=={'outcome':'ticket_submitted','ticket_status':'submitted','owned':False}


def test_timeout_preserves_bounded_redacted_stderr_before_process_stop(tmp_path, monkeypatch):
    module=RUNNER
    cfg={'seat_id':'worker-a','role':'worker','provider':'fake','model':'fake',
         'seat_dir':str(tmp_path),'board_script':str(tmp_path/'board.sh'),
         'state_file':str(tmp_path/'state.json'),'token_file':str(tmp_path/'token'),
         'goose':'/fake/goose','mcp':'/fake/mcp','central_url':'http://localhost/mcp',
         'home_board':'home','repository_root':str(tmp_path/'clones'),
         'max_runs_per_hour':None,'turn_timeout_s':1}
    secret='timeout-secret-assembled-at-runtime'
    Path(cfg['token_file']).write_text(secret);Path(cfg['token_file']).chmod(0o600)
    runner=module['EventSeatRunner'](cfg);runner.active_boards=['home']
    async def refresh(): return None
    runner.refresh_presence=refresh
    runner._active_turn={'board':'home','ticket':'TK-timeout','attempt_id':'attempt-timeout',
                         'claim_ref':'claim-timeout'}
    class Process:
        def __init__(self):self.stderr=io.BytesIO(('request failed '+secret).encode());self.stopped=False;self.waits=0
        def poll(self):return 0 if self.stopped else None
        def wait(self,timeout=None):
            self.waits+=1
            if not self.stopped:raise module['subprocess'].TimeoutExpired(['fake-child'],timeout)
            return 0
        def terminate(self):self.stopped=True
        def kill(self):pytest.fail('cooperative child must terminate')
    process=Process()
    monkeypatch.setattr(module['subprocess'],'Popen',lambda *a,**kw:process)
    with pytest.raises(module['subprocess'].TimeoutExpired):runner._run(['fake-child'])
    evidence=list((tmp_path/'diagnostics').glob('diag-*.json'))
    assert len(evidence)==1
    record=json.loads(evidence[0].read_text())
    assert record['outcome']=='model_timeout' and record['child_outcome']=='model_timeout'
    assert record['error_class']=='TimeoutExpired' and record['returncode'] is None
    assert secret not in evidence[0].read_text() and 'request failed' in record['stderr']
    assert process.stopped and runner._child_result['diagnostic']['ref']==str(evidence[0])


def test_zero_exit_without_submission_retains_evidence_but_is_not_completion(tmp_path, monkeypatch):
    module=RUNNER
    cfg={'seat_id':'worker-a','role':'worker','provider':'fake','model':'fake',
         'seat_dir':str(tmp_path),'board_script':str(tmp_path/'board.sh'),
         'state_file':str(tmp_path/'state.json'),'token_file':str(tmp_path/'token'),
         'goose':'/fake/goose','mcp':'/fake/mcp','central_url':'http://localhost/mcp',
         'home_board':'home','repository_root':str(tmp_path/'clones'),'max_runs_per_hour':None}
    runner=module['EventSeatRunner'](cfg);runner.active_boards=['home']
    async def refresh():return None
    runner.refresh_presence=refresh
    runner._active_turn={'board':'home','ticket':'TK-zero','attempt_id':'attempt-zero','claim_ref':'claim-zero'}
    class Process:
        stderr=io.BytesIO(b'child exited without submitting')
        def wait(self,timeout=None):return 0
        def poll(self):return 0
        def terminate(self):pytest.fail('clean exit must not be terminated')
        def kill(self):pytest.fail('clean exit must not be killed')
    monkeypatch.setattr(module['subprocess'],'Popen',lambda *a,**kw:Process())
    runner._run(['fake-child'])
    runner._apply_child_result({},runner._child_result)
    record=json.loads(Path(runner._child_result['diagnostic']['ref']).read_text())
    assert record['outcome']=='child_exit_zero' and 'without submitting' in record['stderr']
    assert runner._child_result['outcome']=='child_exit_zero'
    assert runner._child_result['diagnostic']['ref'] is not None


def test_runtime_classification_is_published_even_when_child_exits_zero(tmp_path):
    module = RUNNER
    runner = module['EventSeatRunner']({'seat_id': 'worker-a', 'role': 'worker',
        'provider': 'fake', 'model': 'fake', 'seat_dir': str(tmp_path),
        'board_script': str(tmp_path / 'board.sh'), 'state_file': str(tmp_path / 'state.json'),
        'token_file': str(tmp_path / 'token'), 'goose': '/fake/goose', 'mcp': '/fake/mcp',
        'central_url': 'http://localhost/mcp', 'home_board': 'home',
        'repository_root': str(tmp_path / 'clones'), 'max_runs_per_hour': None})
    turn = {'outcome': 'child_exit_zero', 'error_class': None, 'returncode': 0,
            'diagnostic': {'ref': '/private/diag.json', 'outcome': 'provider_unavailable',
                           'omissions': {'stdout': 'not_captured'}}}
    last = {'outcome': 'child_exited'}
    runner._apply_child_result(last, turn)
    assert last['outcome'] == 'provider_unavailable'
    assert last['runtime_outcome'] == 'provider_unavailable'
    assert last['child_outcome'] == 'child_exit_zero'
    assert last['diagnostic_ref'] == '/private/diag.json'
    runner._apply_child_result(last, {'outcome': 'child_exit_zero', 'error_class': None,
                                      'returncode': 0, 'diagnostic': None})
    assert last['outcome'] == 'exited_pending_verification'


def test_technical_classification_requires_positive_provider_or_transport_marker():
    classify = HELPER['classify_failure']
    assert classify('Provider unavailable while starting model') == 'provider_unavailable'
    assert classify('TLS handshake failed: connection reset') == 'transport_failure'
    assert classify('HTTP 401 Unauthorized') == 'authentication_failure'
    assert classify('invalid model response') is None


def test_retention_and_size_bounds_are_enforced(tmp_path):
    state = tmp_path / 'state.json'
    first = HELPER['write_evidence'](state, 'old-attempt', {}, 'old')
    os.utime(first, (1, 1))
    HELPER['write_evidence'](state, 'new-attempt', {}, 'current')
    assert not first.exists()

    path = tmp_path / 'diagnostics' / 'diag-large.json'
    path.write_text('{}')
    path.chmod(0o600)
    with path.open('ab') as handle:
        handle.write(b' ' * (HELPER['MAX_FILE_BYTES'] + 1))
    with pytest.raises(ValueError, match='untrusted'):
        HELPER['write_evidence'](state, 'third-attempt', {}, 'small')
