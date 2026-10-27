"""Reactivation must not infer stopped state from a failed service probe."""
import subprocess

import pytest
from test_fleet_executor import runtime, signed_request, executor
from test_stale_restart import provider


@pytest.mark.parametrize('platform', ['systemd', 'launchd'])
@pytest.mark.parametrize('configured', [False, True])
def test_failed_service_probe_denies_start_before_mutation(runtime, platform, configured):
    provider(runtime)
    calls = []
    def unavailable(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, '', 'probe unavailable')
    root = runtime['tmp_path']
    if platform == 'systemd':
        adapter = executor.SystemdUserAdapter(root/'units', root/'drain',
            runtime['service'].policy.credential_paths, runner=unavailable)
        if configured:
            adapter.unit_dir.mkdir()
            adapter._unit_path('worker-a').write_text(adapter._unit('worker-a', runtime['template']))
    else:
        adapter = executor.LaunchdUserAdapter(root/'agents', root/'drain',
            runtime['service'].policy.credential_paths, runner=unavailable)
        if configured:
            adapter.agent_dir.mkdir()
            adapter._plist_path('worker-a').write_bytes(adapter._plist('worker-a', runtime['template']))
    runtime['service'].adapter = adapter
    receipt = runtime['service'].handle(signed_request(runtime, 'start', 'unknown-probe'))
    assert receipt['outcome'] == 'rejected'
    assert receipt['reason_code'] == 'process_identity_unknown'
    assert runtime['service'].store.seat('worker-a') is None
    assert all(('show' in command if platform == 'systemd' else command[1] == 'print')
               for command in calls)


@pytest.mark.parametrize('active,sub,pid', [('deactivating', 'stop-sigterm', '123'),
    ('unknown', 'dead', '0'), ('inactive', 'dead', '123')])
def test_successful_systemd_query_requires_positive_stopped_state(runtime, active, sub, pid):
    root, template = runtime['tmp_path'], runtime['template']
    def query(command, **kwargs):
        output = (f'LoadState=loaded\nActiveState={active}\nSubState={sub}\nMainPID={pid}\n'
            f'FragmentPath={adapter._unit_path("worker-a")}\nDropInPaths=\n'
            f'ExecStart={{ path={template.command[0]} ; argv[]={template.command[0]} -c '
            '"raise SystemExit(0)" ; ignore_errors=no ; start_time=[n/a] ; }\nControlGroup=\n')
        return subprocess.CompletedProcess(command, 0, output, '')
    adapter = executor.SystemdUserAdapter(root/'units', root/'drain',
        runtime['service'].policy.credential_paths, runner=query)
    adapter.unit_dir.mkdir()
    adapter._unit_path('worker-a').write_text(adapter._unit('worker-a', template))
    observed = adapter.inspect('worker-a', template)
    assert not observed.identity_verified


@pytest.mark.parametrize('state,pid', [('unknown', ''), ('stopped', 'pid = 123\n')])
def test_successful_launchd_query_requires_positive_stopped_state(runtime, state, pid):
    root, template = runtime['tmp_path'], runtime['template']
    def query(command, **kwargs):
        arguments = '\n'.join('\t\t'+arg for arg in adapter._arguments(template))
        output = (f'state = {state}\n{pid}program = {adapter.helper_path}\n'
            f'arguments = {{\n{arguments}\n}}\nworking directory = {template.repository_root}\n')
        return subprocess.CompletedProcess(command, 0, output, '')
    adapter = executor.LaunchdUserAdapter(root/'agents', root/'drain',
        runtime['service'].policy.credential_paths, runner=query)
    adapter.agent_dir.mkdir()
    adapter._plist_path('worker-a').write_bytes(adapter._plist('worker-a', template))
    assert not adapter.inspect('worker-a', template).identity_verified
