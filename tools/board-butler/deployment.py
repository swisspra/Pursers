#!/usr/bin/env python3
"""Render native resident service files without changing installed services."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shlex
import tempfile


REQUIRED = ('python','source','state','url','token_file','home_board','provider_secrets_dir')
FLAGS = {'connector_config':'--connector-config','source_intake_index_file':'--source-intake-index-file',
    'intake_onboarding_config':'--intake-onboarding-config','active_authorization_file':'--active-authorization-file',
    'active_board':'--act-on-board','fleet_observation_file':'--fleet-observation-file',
    'fleet_observation_mode':'--fleet-observation-mode','fleet_local_config':'--fleet-local-config',
    'fleet_executor_state':'--fleet-executor-state','fleet_state_file':'--fleet-state-file',
    'fleet_executor_socket':'--fleet-executor-socket','fleet_executor_key_id':'--fleet-executor-key-id',
    'fleet_executor_private_key':'--fleet-executor-private-key','fleet_executor_config':'--fleet-executor-config',
    'supervisor_roster_file':'--supervisor-roster-file'}


def render_managed_intake_deployment(spec, platform):
    allowed = set(REQUIRED) | set(FLAGS) | {'runtime_mode','git_credentials_config'}
    if set(spec) - allowed or any(not spec.get(k) for k in REQUIRED): raise ValueError('invalid deployment fields')
    if any(not isinstance(v,str) or not v or any(ord(c)<32 for c in v) for v in spec.values()):
        raise ValueError('deployment values must be nonempty single-line strings')
    mode = spec.get('runtime_mode','shadow')
    if mode not in ('shadow','active'): raise ValueError('invalid runtime mode')
    if mode == 'active' and not all(spec.get(k) for k in ('active_authorization_file','active_board')):
        raise ValueError('active mode requires explicit authorization and board')
    state, source = Path(spec['state']), Path(spec['source'])
    if not state.is_absolute() or not source.is_absolute() or state.is_relative_to(source):
        raise ValueError('state must be absolute and outside source checkout')
    argv = [spec['python'], str(source/'tools/board-butler/board_butler.py'), '--url', spec['url'],
            '--token-path', spec['token_file'], '--home-board', spec['home_board'], '--repo', str(source),
            '--pid-file', str(state/'board-butler.pid'), '--cursor-file', str(state/'cursor.json'),
            '--runtime-status-file', str(state/'runtime.json'), '--local-kill-file', str(state/'KILLED'),
            '--provider-secrets-dir', spec['provider_secrets_dir']]
    for key, flag in FLAGS.items():
        if key in spec: argv += [flag,spec[key]]
    argv += ['--runtime-mode',mode]
    env = {'PYTHONPATH':str(source/'packages/client/src')}
    if spec.get('git_credentials_config'):
        helper = '!' + shlex.join([spec['python'],str(source/'tools/ado-connector/git_credential.py'),
                                   '--config',spec['git_credentials_config']])
        # Process-local Git settings; unrelated global configuration is untouched.
        env.update(GIT_CONFIG_COUNT='3', GIT_CONFIG_KEY_0='credential.helper',GIT_CONFIG_VALUE_0='',
                   GIT_CONFIG_KEY_1='credential.helper',GIT_CONFIG_VALUE_1=helper,
                   GIT_CONFIG_KEY_2='credential.useHttpPath',GIT_CONFIG_VALUE_2='true',GIT_TERMINAL_PROMPT='0')
    if platform == 'systemd':
        def quote(v): return '"'+v.replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
        command = ' '.join(quote(v.replace('$','$$')) for v in argv)
        lines = ['[Unit]','Description=Pursers managed intake resident','StartLimitIntervalSec=3600','StartLimitBurst=3',
                 '[Service]','Type=simple','UMask=0077','Restart=no','ExecStart='+command]
        lines += ['Environment='+quote(k+'='+v) for k,v in env.items()]
        lines += ['[Install]','WantedBy=default.target','']
        files = {'pursers-butler.service':'\n'.join(lines)}
    elif platform == 'launchd':
        files = {'com.pursers.butler.plist':plistlib.dumps({'Label':'com.pursers.butler','ProgramArguments':argv,
            'EnvironmentVariables':env,'RunAtLoad':True,'KeepAlive':False,'Umask':63}).decode()}
    else: raise ValueError('unsupported service manager')
    return {'argv':argv,'environment':env,'files':files}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--platform',choices=('systemd','launchd'),required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=render_managed_intake_deployment(json.loads(args.config.read_text()),args.platform)
    args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name,body in result['files'].items():
        fd,tmp=tempfile.mkstemp(dir=args.output,prefix='.render-')
        try:
            with os.fdopen(fd,'w') as f: f.write(body)
            os.replace(tmp,args.output/name)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)


if __name__ == '__main__': main()
