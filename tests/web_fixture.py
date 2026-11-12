"""Render the server shell and API payload in an isolated workspace for assertions."""
from pathlib import Path
import subprocess
import sys


def render_dashboard(root: Path, output: Path) -> subprocess.CompletedProcess[str]:
    source = '''
from pathlib import Path
import sys
sys.path.insert(0, 'tools')
import taskweb
try:
    payload = taskweb.dashboard_payload(api_token='test-token')
    details = [taskweb.dashboard_detail(item['id']) for item in payload['tasks']]
    page = taskweb.build_html(taskweb.bootstrap_config('test-token'))
except ValueError as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(1)
output = Path(sys.argv[1])
output.write_text(page, encoding='utf-8')
output.with_suffix('.json').write_text(taskweb.safe_json(payload), encoding='utf-8')
output.with_suffix('.details.json').write_text(taskweb.safe_json(details), encoding='utf-8')
print(output)
'''
    return subprocess.run([sys.executable, '-c', source, str(output)], cwd=root,
                          text=True, capture_output=True, check=False)
