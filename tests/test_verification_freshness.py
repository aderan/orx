"""Checks belong to the content they observed, not just a Worker attempt."""
import json
import os
from pathlib import Path

import pytest

from orx import dispatch
from conftest import ir_for, task_spec
from test_adapters import bindir, make_bin, make_cli_project


@pytest.fixture
def reviewing(tmp_path, bindir, monkeypatch):
    make_bin(bindir, 'fake-worker', 'mkdir -p src; echo original > src/result.txt\n')
    make_bin(bindir, 'fake-verifier', '''cat > prompt.txt
if grep -q 'exact instruction): first check' prompt.txt; then
 echo ORX_VERDICT=pass
else
 echo 'ORX_VERDICT=pass extra text'
fi
''')
    project = make_cli_project(tmp_path, monkeypatch=monkeypatch)
    goal = dispatch.create_goal(project, 'inspect text', ['text exists'])[0]
    dispatch.submit_plan(project, ir_for(goal, [task_spec(
        'T001', acceptance=['text exists'], verification=[
            'test -f src/result.txt', 'agent: first check', 'agent: second check',
        ], preread=['input.txt']), task_spec(
        'T002', acceptance=['independent'], verification=['true'],
    )]))
    (tmp_path/'input.txt').write_text('input\n')
    dispatch.run_slice(project)
    first = dispatch.verify_dispatch(project)
    assert [v['verdict'] for v in first['launched']] == ['pass', None]
    yield project, bindir
    project.close()


def rows(project, task='T001'):
    _, run=dispatch._active_context(project)
    rev=project.store.revision_active(run.id)
    return project.store.verifications_for(rev.id, task)


def resume(project, bindir):
    make_bin(bindir, 'fake-verifier', 'echo ORX_VERDICT=pass\n')
    return dispatch.verify_dispatch(project)


def test_unchanged_content_only_retries_pending_entry(reviewing):
    p,b=reviewing
    old=rows(p)
    path=p.root/'src/result.txt'
    os.utime(path, None)
    result=resume(p,b)
    assert [v['entry'] for v in result['launched']]==['agent: second check']
    assert rows(p)[:len(old)]==old
    assert len(rows(p))==len(old)+1


def test_output_changed_invalidates_all_checks_of_that_task(reviewing):
    p,b=reviewing
    old=rows(p)
    path=p.root/'src/result.txt';stamp=path.stat()
    path.write_text('modified\n')  # same size as original, restore mtime too
    os.utime(path, ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    result=resume(p,b)
    assert [v['entry'] for v in result['launched']]==['agent: first check','agent: second check']
    assert len([v for v in rows(p) if v.kind=='command'])==2
    assert rows(p)[:len(old)]==old
    assert result['invalidated'][0]['task']=='T001'
    assert 'src/' in ' '.join(result['invalidated'][0]['changed_paths'])


def test_preread_changed_invalidates_previous_verdict(reviewing):
    p,b=reviewing
    (p.root/'input.txt').write_text('different\n')
    result=resume(p,b)
    assert [v['entry'] for v in result['launched']]==['agent: first check','agent: second check']
    assert 'input.txt' in ' '.join(result['invalidated'][0]['changed_paths'])


def test_unrelated_file_does_not_invalidate(reviewing):
    p,b=reviewing
    (p.root/'unrelated.txt').write_text('different\n')
    result=resume(p,b)
    assert [v['entry'] for v in result['launched']]==['agent: second check']


def test_deletion_then_restoration_never_revives_old_rows(reviewing):
    p,b=reviewing
    original=(p.root/'src/result.txt').read_bytes()
    old=rows(p)
    (p.root/'src/result.txt').unlink()
    failed=resume(p,b)
    assert failed['checked'][0]['verdict']=='failed'
    assert failed['launched']==[]
    (p.root/'src/result.txt').write_bytes(original)
    result=resume(p,b)
    assert [v['entry'] for v in result['launched']]==['agent: first check','agent: second check']
    assert rows(p)[:len(old)]==old


def test_status_reports_staleness_without_writing(reviewing):
    p,b=reviewing
    (p.root/'input.txt').write_text('new input')
    before=p.store.conn.total_changes
    result=dispatch.status_data(p)
    assert result['evidence_freshness']['stale'][0]['task']=='T001'
    assert p.store.conn.total_changes==before


def test_cli_result_from_changed_inputs_is_not_a_verdict(reviewing):
    p,b=reviewing
    make_bin(b,'fake-verifier','echo new > input.txt; echo ORX_VERDICT=pass\n')
    result=dispatch.verify_dispatch(p)
    entry=result['launched'][0]
    assert entry['verdict'] is None
    assert entry['error_kind']=='evidence_changed'
    assert entry['retryable'] is True
    assert dispatch.task_list(p)[0]['status']=='verifying'
    again=resume(p,b)
    assert [v['entry'] for v in again['launched']]==['agent: first check','agent: second check']


def test_completed_run_reopens_only_affected_task(reviewing):
    p,b=reviewing
    resume(p,b)
    dispatch.run_slice(p)  # independent T002
    assert dispatch.status_data(p)['run']['status']=='done'
    independent=rows(p,'T002')
    (p.root/'input.txt').write_text('new input')
    result=resume(p,b)
    assert [v['task'] for v in result['launched']]==['T001','T001']
    assert rows(p,'T002')==independent
    assert dispatch.status_data(p)['run']['status']=='done'


@pytest.fixture
def chain(tmp_path, monkeypatch):
    from conftest import make_project, write_evidence
    monkeypatch.chdir(tmp_path)
    p=make_project(tmp_path)
    goal=dispatch.create_goal(p,'dependency chain',['done'])[0]
    tasks=[task_spec('T001',acceptance=['done'],allowed=['producer.txt'],
                     preread=['config.txt'],verification=['test -f producer.txt']),
           task_spec('T002',deps=['T001'],allowed=['consumer.txt'],
                     verification=['test -f consumer.txt','agent: inspect consumer']),
           task_spec('T003',deps=['T002'],allowed=['third.txt'],
                     verification=['test -f third.txt','agent: inspect third'])]
    dispatch.submit_plan(p,ir_for(goal,tasks))
    (tmp_path/'config.txt').write_text('one\n')
    def complete(task_id,path):
        dispatch.run_slice(p)
        dispatch.task_claim(p,task_id)
        (tmp_path/path).write_text('delivered\n')
        evidence=write_evidence(tmp_path,name=task_id+'.json',artifacts=[path])
        dispatch.task_complete(p,task_id,str(evidence))
    complete('T001','producer.txt')
    yield p,complete
    p.close()


def pass_host_entries(p):
    required=dispatch.verify_dispatch(p)['agent_required']
    for entry in required:
        if not entry.get('error'):
            dispatch.verify_submit(p,entry['task'],'pass',entry['entry'],None,attempt_id=entry['attempt'])
    return required


def test_dependency_drift_prevents_dispatch_using_old_pass(chain):
    p,complete=chain
    old=p.store.attempts_all()
    (p.root/'producer.txt').write_text('changed')
    result=dispatch.run_slice(p)
    assert result['host_required']==[] and result['started']==[]
    assert result['deferred'][0]['blocked_by']==['T001']
    assert p.store.attempts_all()==old
    dispatch.verify_dispatch(p)
    assert dispatch.run_slice(p)['host_required'][0]['task']=='T002'


def test_preread_drift_propagates_through_unchanged_dependency_artifacts(chain):
    p,complete=chain
    complete('T002','consumer.txt');pass_host_entries(p)
    complete('T003','third.txt');pass_host_entries(p)
    old_workers=[a for a in p.store.attempts_all() if a.role=='worker']
    old_history={t:rows(p,t) for t in ['T001','T002','T003']}
    assert dispatch.status_data(p)['run']['status']=='done'
    (p.root/'config.txt').write_text('two\n')
    result=dispatch.verify_dispatch(p)
    assert {v['task'] for v in result['invalidated']}=={'T001','T002','T003'}
    assert result['agent_required'][0]['task']=='T002'
    assert any(v.get('blocked_by')==['T002'] for v in result['checked'])
    pass_host_entries(p)
    pass_host_entries(p)
    assert dispatch.status_data(p)['run']['status']=='done'
    assert [a for a in p.store.attempts_all() if a.role=='worker']==old_workers
    for task,history in old_history.items():
        assert rows(p,task)[:len(history)]==history


def test_inflight_host_verifier_rejects_stale_pass_without_duplicate_dispatch(chain):
    p,complete=chain
    complete('T002','consumer.txt')
    entry=dispatch.verify_dispatch(p)['agent_required'][0]
    (p.root/'producer.txt').write_text('changed')
    again=dispatch.verify_dispatch(p)['agent_required'][0]
    assert again['attempt']==entry['attempt']
    assert again['error_kind']=='evidence_changed'
    assert p.store.attempt_get(entry['attempt']).ended_at is None
    verdict=dispatch.verify_submit(p,'T002','pass',entry['entry'],None,attempt_id=entry['attempt'])
    assert verdict['verdict'] is None and verdict['error_kind']=='evidence_changed'
    assert p.store.attempt_get(entry['attempt']).ended_at is not None
    assert not [v for v in rows(p,'T002') if v.kind=='agent' and v.attempt_id==entry['attempt']]
    fresh=dispatch.verify_dispatch(p)['agent_required'][0]
    assert fresh['attempt']!=entry['attempt']


def test_untracked_historical_results_require_one_fresh_check(chain):
    p,complete=chain
    rev=p.store.revision_active(dispatch._active_context(p)[1].id)
    # Simulate a pre-upgrade database; production code never deletes history.
    p.store.conn.execute("DELETE FROM task_events WHERE event='verification_context'")
    old=rows(p)
    result=dispatch.verify_dispatch(p)
    assert result['invalidated'][0]['reason']=='untracked'
    assert len(rows(p))==len(old)+1
    assert p.store.verifications_current(rev.id,'T001')[-1].id!=old[-1].id


def test_directory_runtime_cache_is_not_a_source_change(reviewing):
    p,b=reviewing
    cache=p.root/'src/__pycache__';cache.mkdir()
    (cache/'compiled.pyc').write_bytes(b'cache')
    assert [v['entry'] for v in resume(p,b)['launched']]==['agent: second check']


def test_outside_symlink_invalidates_and_blocks_without_reading_target(reviewing, monkeypatch):
    p,b=reviewing
    outside=p.root.parent/'outside-private.txt';outside.write_text('private')
    artifact=p.root/'src/result.txt';artifact.unlink();artifact.symlink_to(outside)
    real_open=Path.open
    def checked_open(path,*args,**kwargs):
        assert path.resolve()!=outside.resolve(), 'external content must not be read'
        return real_open(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',checked_open)
    result=resume(p,b)
    assert result['launched']==[]
    assert result['checked'][0]['reason']=='verification input unavailable'
    assert result['invalidated'][0]['verification_ids']


def test_command_log_paths_remain_unique_across_revalidations(chain):
    p,complete=chain
    (p.root/'producer.txt').write_text('two')
    dispatch.verify_dispatch(p)
    logs=[(p.root/v.output_path,(p.root/v.output_path).read_bytes()) for v in rows(p)]
    (p.root/'producer.txt').write_text('three')
    dispatch.verify_dispatch(p)
    paths=[v.output_path for v in rows(p)]
    assert len(set(paths))==len(paths)
    assert all(path.read_bytes()==content for path,content in logs)


def test_parked_consumer_cannot_claim_after_dependency_drift(chain):
    from orx.records import ConflictError
    p,complete=chain
    parked=dispatch.run_slice(p)['host_required'][0]
    (p.root/'producer.txt').write_text('changed')
    with pytest.raises(ConflictError,match='dependency verification pending'):
        dispatch.task_claim(p,'T002')
    assert p.store.attempt_get(parked['attempt']).started_at is None
    surfaced=dispatch.run_slice(p)['host_required'][0]
    assert surfaced['execution']['blocked_by']==['T001']


def test_live_status_does_not_count_stale_passes(reviewing):
    p,b=reviewing
    resume(p,b)
    (p.root/'input.txt').write_text('changed')
    status=dispatch.status_data(p)
    assert status['verification']['passed']==0
    assert status['result']=='verification_stale'
    assert status['next_actions'][0]['action']=='verify'


def test_corrupt_context_is_an_explicit_error_not_reusable_history(reviewing):
    from orx.records import ORXError
    p,b=reviewing
    p.store.conn.execute("UPDATE task_events SET reason='{}' WHERE event='verification_context'")
    with pytest.raises(ORXError,match='cannot reuse checks'):
        dispatch.verify_dispatch(p)


def test_glob_output_scope_tracks_content(reviewing):
    p,b=reviewing
    # Existing task contract may contain glob scopes; fingerprint changes
    # must be noticed even when the path spec is not a literal file.
    p.store.conn.execute("UPDATE tasks SET scope_json=? WHERE task_id='T001'",
                         (json.dumps({'allowed':['src/*.txt']}),))
    (p.root/'src/result.txt').write_text('changed')
    result=resume(p,b)
    assert [v['entry'] for v in result['launched']]==['agent: first check','agent: second check']


def test_replayed_delivery_receipt_does_not_resurrect_invalidated_checks(chain):
    p,complete=chain
    worker=p.store.attempts_all()[0]
    receipt=p.store.completion_receipt_for_attempt(worker.id).receipt
    (p.root/'producer.txt').unlink()
    dispatch.verify_dispatch(p)
    before=rows(p)
    replay=dispatch.task_complete(p,'T001',str(p.root/'T001.json'),attempt_id=worker.id)
    assert replay==receipt
    assert dispatch.task_list(p)[0]['status']=='failed'
    assert rows(p)==before


def test_commands_that_change_inputs_cannot_leave_a_reusable_pass(tmp_path, bindir, monkeypatch):
    make_bin(bindir,'fake-worker','mkdir -p src; echo original > src/result.txt; echo input > input.txt\n')
    make_bin(bindir,'fake-verifier','echo ORX_VERDICT=pass\n')
    p=make_cli_project(tmp_path,monkeypatch=monkeypatch)
    try:
        goal=dispatch.create_goal(p,'verify stable input',['done'])[0]
        check="if test -f mutate.flag; then echo changed >> input.txt; fi; test -f src/result.txt"
        dispatch.submit_plan(p,ir_for(goal,[task_spec('T001',acceptance=['done'],preread=['input.txt'],
                                                    verification=[check,'agent: inspect input'])]))
        dispatch.run_slice(p)
        (tmp_path/'input.txt').write_text('new input')
        (tmp_path/'mutate.flag').touch()
        changed=dispatch.verify_dispatch(p)
        assert changed['launched']==[] and changed['checked'][0]['verdict']=='pending'
        (tmp_path/'mutate.flag').unlink()
        stable=dispatch.verify_dispatch(p)
        assert stable['launched'][0]['verdict']=='pass'
    finally:
        p.close()


def test_new_delivery_does_not_inherit_unbound_legacy_pass(chain):
    p,complete=chain
    rev=p.store.revision_active(dispatch._active_context(p)[1].id)
    old=p.store.verification_add(rev.id,'T002','agent','agent: inspect consumer',passed=True,attempt_id=None)
    complete('T002','consumer.txt')
    required=dispatch.verify_dispatch(p)['agent_required']
    assert required[0]['entry']=='agent: inspect consumer'
    assert old in rows(p,'T002')
    assert old not in p.store.verifications_current(rev.id,'T002')


def test_delivery_gate_excludes_earlier_self_checks_on_unfinished_content(chain):
    p,complete=chain
    dispatch.run_slice(p);dispatch.task_claim(p,'T002')
    failed=dispatch.task_check(p,'T002')
    assert failed['summary']['failed']==1
    (p.root/'consumer.txt').write_text('done')
    from conftest import write_evidence
    evidence=write_evidence(p.root,name='final.json',artifacts=['consumer.txt'])
    dispatch.task_complete(p,'T002',str(evidence))
    rev=p.store.revision_active(dispatch._active_context(p)[1].id)
    current=p.store.verifications_current(rev.id,'T002')
    assert all(v.passed for v in current)
    assert not ({r['verification_id'] for r in failed['results']} & {v.id for v in current})
    assert len(rows(p,'T002'))>len(current)
