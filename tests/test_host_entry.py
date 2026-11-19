"""Current Controller identity is invocation-scoped, not project routing."""
import json

import pytest
from typer.testing import CliRunner

from orx import config, dispatch, routing
from orx.cli import app
from orx.records import ConfigError

runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


@pytest.mark.parametrize('host', ['codex', 'zcode', 'cursor', 'claude'])
def test_fresh_init_works_with_declared_current_host(tmp_path, monkeypatch, host):
    monkeypatch.chdir(tmp_path)
    invoke('--host', host, 'init', '--json')
    raw = config.load_profiles(tmp_path / '.orx/profiles.toml')['orx-host']
    assert raw.harness.value == 'current'
    before = (tmp_path / '.orx/profiles.toml').read_bytes()
    selection = invoke('--host', host, 'config', 'models', '--json')['model_selection']
    candidate = selection['worker']['candidates'][0]
    assert candidate['harness'] == host
    assert candidate['declared_harness'] == 'current'
    assert candidate['host_blocked_reason'] is None
    invoke('goal', 'new', '--objective', 'write a marker', '--acceptance', 'marker exists', '--json')
    plan = invoke('--host', host, 'plan', '--json')
    assert plan['assignment']['execution']['harness'] == host
    assert (tmp_path / '.orx/profiles.toml').read_bytes() == before


def test_host_flags_replace_environment_without_leaking(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('ORX_CONTROLLER_HARNESS', 'zcode')
    monkeypatch.setenv('ORX_CONTROLLER_AGENTS', 'orx-worker')
    invoke('init', '--json')
    value = invoke('--host', 'codex', '--host-agent', 'researcher', '--host-session', 'real-session',
                   'config', 'models', '--json')['model_selection']['controller']
    assert value['session_harness'] == 'codex'
    assert value['native_agents'] == ['researcher']
    assert value['session_ref'] == 'real-session'
    assert value['source'] == 'cli'
    legacy = invoke('config', 'models', '--json')['model_selection']['controller']
    assert legacy['session_harness'] == 'zcode'
    assert legacy['native_agents'] == ['orx-worker']
    assert routing.ControllerContext.from_environment().harness == 'zcode'


def test_flag_host_does_not_inherit_other_hosts_agents(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('ORX_CONTROLLER_HARNESS', 'zcode')
    monkeypatch.setenv('ORX_CONTROLLER_AGENTS', 'orx-worker')
    invoke('init', '--json')
    c = invoke('--host', 'codex', 'config', 'models', '--json')['model_selection']['controller']
    assert c['native_agents'] == []


@pytest.mark.parametrize('args', [('--host-agent', 'worker'), ('--host-session', 's'),
                                 ('--host', 'current'), ('--host', 'codex', '--host-agent', ' '),
                                 ('--host', 'codex', '--host-session', 'bad session')])
def test_bad_host_declaration_fails_before_init(tmp_path, args):
    result = runner.invoke(app, [*args, 'init', str(tmp_path), '--json'])
    assert result.exit_code == 2
    assert not (tmp_path / '.orx').exists()


def test_missing_host_explains_current_profile_blocker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('ORX_CONTROLLER_HARNESS', raising=False)
    invoke('init', '--json')
    invoke('goal', 'new', '--objective', 'marker', '--acceptance', 'marker exists', '--json')
    selection = invoke('config', 'models', '--json')['model_selection']
    assert '--host' in selection['worker']['candidates'][0]['host_blocked_reason']
    result = runner.invoke(app, ['plan', '--json'])
    assert result.exit_code == 1 and '--host' in result.stdout


def test_switching_host_keeps_existing_planner_owner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    invoke('init', '--json')
    invoke('goal', 'new', '--objective', 'marker', '--acceptance', 'marker exists', '--json')
    first = invoke('--host', 'codex', 'plan', '--json')
    wrong = runner.invoke(app, ['--host', 'zcode', 'plan', '--json'])
    assert wrong.exit_code == 1 and 'current zcode' in wrong.stdout
    resumed = invoke('--host', 'codex', 'plan', '--json')
    assert resumed['assignment']['execution']['attempt'] == first['assignment']['execution']['attempt']


@pytest.mark.parametrize('declaration', ['driver = "cli"', 'host_mode = "subagent"\nagent_ref = "worker"'])
def test_current_harness_is_only_for_host_self(tmp_path, declaration):
    text = dispatch.DEFAULT_PROFILES_TOML
    text = text.replace('driver = "host"', declaration) if declaration.startswith('driver') else text + '\n' + declaration
    p = tmp_path/'profiles.toml'; p.write_text(text)
    with pytest.raises(ConfigError, match='current.*host.*self'):
        config.load_profiles(p)


def test_cli_preset_is_independent_of_controller_and_installs_no_native_roles(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke('preset', 'install', 'cli', '--json')
    assert result['agents'] == []
    invoke('init', '--json')
    assert not (tmp_path/'.orx/config.toml').exists()
    for host in ['codex', 'zcode']:
        s = invoke('--host', host, 'config', 'models', '--json')['model_selection']
        assert [p['profile'] for p in s['worker']['candidates']] == ['zcode-cli-worker', 'cursor-cli-worker', 'orx-host']
        assert all(p['host_blocked_reason'] is None for p in s['worker']['candidates'])


@pytest.mark.parametrize('host', ['codex', 'zcode', 'cursor', 'claude'])
def test_skill_install_targets_explicit_host_in_clean_home(tmp_path, host):
    home = tmp_path/'install-home'
    result = invoke('--host', host, 'skill', 'install', '--home', str(home), '--json')
    assert (home/f'.{host}/skills/orx-controller').is_symlink()
    assert (home/f'.{host}/skills/orx-agent/SKILL.md').is_file()
    assert result['canonical_root'] == str(home/'.agents/skills')
    assert not (home/f'.{host}/skills/orx-pbv').exists()
    for other in {'codex','zcode','cursor','claude'} - {host}:
        assert not (home/f'.{other}').exists()


def test_native_agent_list_filters_only_host_candidates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    invoke('preset', 'install', 'cli', '--json')
    invoke('init', '--json')
    p = config.user_profiles_path()
    with p.open('a') as f:
        f.write('''
[profiles.native-codex]
driver = "host"
harness = "codex"
model = "unconfigured"
tier = "T2"
effort = "medium"
host_mode = "subagent"
agent_ref = "builder"
capabilities = ["coding"]
''')
    invoke('config', 'set', '--json', 'worker.profiles', '["native-codex", "cursor-cli-worker"]')
    before = p.read_bytes()
    for flags, blocked in [(['--host','codex'], True),
                            (['--host','codex','--host-agent','builder'], False),
                            (['--host','zcode','--host-agent','builder'], True)]:
        rows = invoke(*flags, 'config','models','--json')['model_selection']['worker']['candidates']
        assert bool(rows[0]['host_blocked_reason']) == blocked
        assert rows[1]['host_blocked_reason'] is None
    assert p.read_bytes() == before


def test_current_host_parked_worker_keeps_its_owner(tmp_path, monkeypatch):
    from conftest import ir_for, task_spec
    monkeypatch.chdir(tmp_path)
    invoke('init','--json')
    invoke('goal','new','--objective','marker','--acceptance','marker exists','--json')
    project = dispatch.open_project()
    try:
        actual_goal = project.store.goal_active()
        dispatch.submit_plan(project, ir_for(actual_goal,[task_spec('T001',acceptance=['marker exists'])]))
    finally:
        project.close()
    first = invoke('--host','codex','run','--json')['host_required'][0]
    second = invoke('--host','zcode','run','--json')['host_required'][0]
    assert second['execution']['attempt'] == first['attempt']
    assert second['execution']['harness'] == 'codex'
    assert 'current zcode' in second['execution']['blocked_reason']


def test_declared_controller_session_is_not_guessed_from_profile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    invoke('init','--json')
    invoke('goal','new','--objective','marker','--acceptance','marker exists','--json')
    planned = invoke('--host','codex','--host-session','actual-controller', 'plan','--json')
    project = dispatch.open_project()
    try:
        attempt = project.store.attempt_get(planned['assignment']['execution']['attempt'])
        assert attempt.session_ref == 'actual-controller'
        assert attempt.harness == 'codex'
    finally:
        project.close()


def test_skill_update_targets_declared_host_without_reinstalling_pbv(tmp_path):
    home=tmp_path/'home'
    invoke('--host','zcode','skill','install','--home',str(home),'--json')
    invoke('--host','codex','skill','update','--home',str(home),'--json')
    assert (home/'.codex/skills/orx-controller/SKILL.md').is_file()
    assert not (home/'.agents/skills/orx-pbv').exists()


def test_next_action_can_be_executed_without_environment_flags(tmp_path, monkeypatch):
    import shlex
    monkeypatch.chdir(tmp_path)
    invoke('init','--json')
    invoke('goal','new','--objective','marker','--acceptance','marker exists','--json')
    status = invoke('--host','codex','--host-agent','builder', '--host-session','actual-id','status','--json')
    command = status['next_actions'][0]['command']
    planned = invoke(*shlex.split(command)[1:])
    assert planned['assignment']['execution']['harness']=='codex'
    project=dispatch.open_project()
    try:
        attempt=project.store.attempt_get(planned['assignment']['execution']['attempt'])
        assert attempt.session_ref=='actual-id'
    finally:
        project.close()


def test_relative_skill_home_creates_resolvable_links(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result=invoke('--host','codex','skill','install','--home','install-home','--json')
    link=tmp_path/'install-home/.codex/skills/orx-controller'
    assert (link/'SKILL.md').is_file()
    assert result['canonical_root']==str(tmp_path/'install-home/.agents/skills')
