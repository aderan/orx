"""Thin acceptance runner; the current Codex conversation chooses every command."""
import json
import os
from pathlib import Path
import subprocess
import sys

lab = Path(sys.argv[1]).resolve()
output = lab / sys.argv[2]
env = {k:v for k,v in os.environ.items() if not k.startswith('ORX_')}
env.update(ORX_HOME=str(lab/'user-config'), ORX_DATA_DIR=str(lab/'user-data'))
result = subprocess.run([str(lab/'orx-env/bin/orx'), '--host', 'codex', *sys.argv[3:]],
                        cwd=lab, env=env, capture_output=True, text=True)
output.write_text(result.stdout)
output.with_suffix('.stderr').write_text(result.stderr)
try:
    value = json.loads(result.stdout)
except ValueError:
    print(result.stdout[-1200:], result.stderr[-1200:])
    raise SystemExit(result.returncode or 1)
print({k:value[k] for k in ['ok','error','mode','revision','tasks','run','goal'] if k in value})
for field in ['started', 'launched', 'host_required', 'routing_errors']:
    if value.get(field):
        print(field, [{k:r.get(k) for k in ['task','attempt','profile','status','verdict','error_kind','detail']} for r in value[field]])
raise SystemExit(result.returncode)
