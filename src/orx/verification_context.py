"""Content versions for task verification, persisted as append-only task events.

A version covers all checks of one task. Plan entries do not declare individual
file dependencies, so a changed task input invalidates that task's whole check
set, while unrelated task results remain usable. Historical rows are never edited.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from orx.state import Store, TaskRow

# Runtime bookkeeping is not an input to a directory-scoped source check.
_IGNORED = frozenset({'.orx', '.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'})


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def _path_state(root: Path, relative: str, ancestors: frozenset[Path] = frozenset(),
                *, expand_glob: bool = True) -> dict:
    path = root / relative
    try:
        if expand_glob and any(char in relative for char in '*?['):
            prefix = []
            for part in Path(relative).parts:
                if any(char in part for char in '*?['):
                    break
                prefix.append(part)
            if not (root.joinpath(*prefix)).resolve().is_relative_to(root):
                return {'kind': 'unavailable', 'reason': 'outside_workspace'}
            matches = {str(p.relative_to(root)): _path_state(root, str(p.relative_to(root)), ancestors, expand_glob=False)
                       for p in sorted(root.glob(relative)) if not (_IGNORED & set(p.relative_to(root).parts))}
            if any(_unavailable(value) for value in matches.values()):
                return {'kind': 'unavailable', 'reason': 'unreadable_glob_entry'}
            return {'kind': 'glob', 'sha256': _digest(matches)}
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            return {'kind': 'unavailable', 'reason': 'outside_workspace'}
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            if resolved in ancestors:
                return {'kind': 'unavailable', 'reason': 'symlink_cycle'}
            return {'kind': 'symlink', 'target': os.readlink(path),
                    'value': _path_state(root, str(resolved.relative_to(root)), ancestors, expand_glob=False)}
        if stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(block)
            after = path.stat()
            if (info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                return {'kind': 'unavailable', 'reason': 'changed_while_reading'}
            return {'kind': 'file', 'sha256': digest.hexdigest(), 'mode': stat.S_IMODE(info.st_mode)}
        if stat.S_ISDIR(info.st_mode):
            if resolved in ancestors:
                return {'kind': 'unavailable', 'reason': 'symlink_cycle'}
            children = {child.name: _path_state(root, str(child.relative_to(root)), ancestors | {resolved}, expand_glob=False)
                        for child in sorted(path.iterdir()) if child.name not in _IGNORED}
            if any(_unavailable(value) for value in children.values()):
                return {'kind': 'unavailable', 'reason': 'unreadable_directory_entry'}
            return {'kind': 'directory', 'sha256': _digest(children)}
        return {'kind': 'unavailable', 'reason': 'not_regular_file_or_directory'}
    except FileNotFoundError:
        return {'kind': 'missing'}
    except (OSError, ValueError, RuntimeError):
        return {'kind': 'unavailable', 'reason': 'unreadable'}


def _unavailable(value: dict) -> bool:
    return value.get('kind') == 'unavailable' or (
        value.get('kind') == 'symlink' and _unavailable(value['value']))


def _artifact_paths(store: Store, revision_id: int, task: TaskRow) -> tuple[int | None, list[str]]:
    worker = store.attempt_current_worker_for_task(revision_id, task.task_id)
    paths = [row.path for row in store.evidence_rows_for_task(revision_id, task.task_id)
             if worker and row.attempt_id == worker.id and row.kind == 'delivery_artifact']
    # Legacy exit-channel workers have no structured artifact declaration.
    if not paths:
        paths = task.scope.get('allowed', [])
    return worker.id if worker else None, sorted(set(paths))


def capture(store: Store, root: Path, revision_id: int, task: TaskRow) -> dict:
    root = root.resolve()
    worker, artifacts = _artifact_paths(store, revision_id, task)
    paths = {f'output:{p}': _path_state(root, p) for p in artifacts}
    paths.update({f'preread:{p}': _path_state(root, p) for p in sorted(set(task.preread))})
    dependencies = {}
    for dep in sorted(store.deps_for(revision_id, task.task_id)):
        source = store.task_get(revision_id, dep)
        attempt, outputs = _artifact_paths(store, revision_id, source)
        context = store.verification_context(revision_id, dep)
        dependencies[dep] = {'worker_attempt': attempt, 'context_digest': (
            context['digest'] if context and context['worker_attempt'] == attempt else None)}
        paths.update({f'dependency:{dep}:{p}': _path_state(root, p) for p in outputs})
    manifest = {'worker_attempt': worker, 'revision_id': revision_id, 'task': task.task_id,
                'contract': {'acceptance': task.acceptance, 'verification': task.verification,
                             'scope': task.scope}, 'paths': paths, 'dependencies': dependencies}
    return {'digest': _digest(manifest), 'manifest': manifest,
            'reusable': not any(_unavailable(value) for value in paths.values())}


def inspect(store: Store, root: Path, revision_id: int, task: TaskRow) -> dict:
    current = capture(store, root, revision_id, task)
    old = store.verification_context(revision_id, task.task_id)
    if old and old['worker_attempt'] != current['manifest']['worker_attempt']:
        old = None
    before = old['manifest'] if old else {}
    old_paths = before.get('paths', {})
    new_paths = current['manifest']['paths']
    changed = [p for p in sorted(old_paths.keys() | new_paths.keys())
               if old_paths.get(p) != new_paths.get(p)]
    for dep, value in current['manifest']['dependencies'].items():
        if before.get('dependencies', {}).get(dep) != value:
            changed.append(f'dependency:{dep}')
    state = 'current' if old and old['digest'] == current['digest'] else ('stale' if old else 'untracked')
    if not current['reusable']:
        state = 'unavailable'
    return {'task': task.task_id, 'state': state, 'context': old,
            'current': current, 'changed_paths': changed}


def synchronize(store: Store, root: Path, revision_id: int, task: TaskRow,
                *, accepted_delivery: bool = False, accepted_rows: list[int] | None = None) -> dict:
    """Move the reuse boundary only forward. A reverted file cannot revive old rows.

    Fresh delivery gates may be adopted once at acceptance. Historical checks
    lacking a baseline are otherwise rechecked rather than grandfathered in.
    """
    with store.tx():
        observed = inspect(store, root, revision_id, task)
        if observed['state'] == 'current' or (observed['context'] and
                observed['context']['digest'] == observed['current']['digest'] and
                not observed['current']['reusable']):
            return {**observed, 'invalidated_ids': [], 'changed': False}
        old = observed['context']
        rows = store.verifications_current(revision_id, task.task_id)
        previous_floor = old['verification_after'] if old else 0
        if accepted_delivery:
            # Earlier same-Worker self-checks can describe unfinished inputs.
            # Only the fresh delivery gate belongs to the accepted version.
            floor = (min(accepted_rows) - 1 if accepted_rows else
                     max([previous_floor, *(row.id for row in rows)]))
            floor = max(previous_floor, floor)
        else:
            floor = max([previous_floor, *(row.id for row in rows)])
        snapshot = observed['current']
        payload = {'version': 1, 'worker_attempt': snapshot['manifest']['worker_attempt'],
                   'digest': snapshot['digest'], 'manifest': snapshot['manifest'],
                   'verification_after': floor, 'reusable': snapshot['reusable'],
                   'invalidated_ids': [row.id for row in rows if row.id <= floor],
                   'changed_paths': observed['changed_paths'],
                   'reason': 'accepted_delivery' if accepted_delivery else observed['state']}
        store.task_event_add(revision_id, task.task_id, task.status, task.status,
                             'verification_context', json.dumps(payload, sort_keys=True))
        return {**observed, 'context': store.verification_context(revision_id, task.task_id),
                'invalidated_ids': payload['invalidated_ids'], 'changed': True}


def bind_attempt(store: Store, revision_id: int, task: TaskRow, attempt_id: int,
                 context_id: int | None = None) -> None:
    context = store.verification_context(revision_id, task.task_id)
    store.task_event_add(revision_id, task.task_id, task.status, task.status,
                         'verification_bound', json.dumps({'attempt': attempt_id,
                                                          'context': context_id if context_id is not None else
                                                          (context['id'] if context else None)}))


def attempt_is_current(store: Store, revision_id: int, task_id: str, attempt_id: int) -> bool:
    context = store.verification_context(revision_id, task_id)
    for event in reversed(store.task_events(revision_id, task_id)):
        if event.event == 'verification_bound':
            binding = json.loads(event.reason)
            if binding['attempt'] == attempt_id:
                return context is not None and binding['context'] == context['id'] and context['reusable']
    return False


def ordered_tasks(store: Store, revision_id: int) -> list[TaskRow]:
    tasks = {task.task_id: task for task in store.tasks_all(revision_id)}
    ordered = []
    visited = set()
    def visit(task_id):
        if task_id in visited:
            return
        visited.add(task_id)
        for dep in store.deps_for(revision_id, task_id):
            visit(dep)
        ordered.append(tasks[task_id])
    for task_id in tasks:
        visit(task_id)
    return ordered
