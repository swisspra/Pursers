import json
import os
import runpy
import subprocess
from pathlib import Path


def test_launch_forwards_native_intake_configuration(tmp_path):
    root=Path(__file__).resolve().parents[3]
    fake=tmp_path/'python';fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n');fake.chmod(0o755)
    env={**os.environ, 'PURSERS_BUTLER_PYTHON':str(fake),'PURSERS_BUTLER_REPO':str(root),
         'PURSERS_BUTLER_URL':'http://127.0.0.1:8766/mcp','PURSERS_BUTLER_TOKEN_PATH':str(tmp_path/'token'),
         'PURSERS_BUTLER_STATE_DIR':str(tmp_path/'state'),'PURSERS_BUTLER_PROVIDER_SECRETS_DIR':str(tmp_path/'secrets'),
         'PURSERS_BUTLER_HOME_BOARD':'home','PURSERS_BUTLER_CONNECTOR_CONFIG':str(tmp_path/'connectors.json'),
         'PURSERS_BUTLER_SOURCE_INTAKE_INDEX_FILE':str(tmp_path/'index.json'),
         'PURSERS_BUTLER_INTAKE_ONBOARDING_CONFIG':str(tmp_path/'onboarding.json')}
    result=subprocess.run(['sh',str(root/'tools/board-butler/launch.sh')],env=env,capture_output=True,text=True,check=True)
    assert '--connector-config\n'+str(tmp_path/'connectors.json') in result.stdout
    assert '--source-intake-index-file\n'+str(tmp_path/'index.json') in result.stdout
    assert '--intake-onboarding-config\n'+str(tmp_path/'onboarding.json') in result.stdout


def test_deployment_renderer_is_repeatable_and_shadow_by_default(tmp_path):
    path=Path(__file__).resolve().parents[1]/'deployment.py'
    assert path.exists(), 'deployment renderer is missing'
    render=runpy.run_path(str(path))['render_managed_intake_deployment']
    spec={'python':'/PATH/TO/venv/bin/python','source':'/PATH/TO/source','state':'/PATH/TO/state',
          'url':'https://central.example/mcp','token_file':'/PATH/TO/auth/token','home_board':'home',
          'provider_secrets_dir':'/PATH/TO/auth','connector_config':'/PATH/TO/config/connectors.json',
          'source_intake_index_file':'/PATH/TO/state/index.json','intake_onboarding_config':'/PATH/TO/config/onboarding.json'}
    result=render(spec,'systemd')
    assert result == render(spec,'systemd')
    assert result['argv'][-2:] == ['--runtime-mode','shadow']
    assert 'UMask=0077' in result['files']['pursers-butler.service']
