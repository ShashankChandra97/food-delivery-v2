"""Start the local support server without keeping the launcher open."""
from pathlib import Path
import subprocess
import sys
import urllib.request

root = Path(__file__).resolve().parent
try:
    with urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=2) as response:
        print('Local service already running:', response.status)
except Exception:
    (root/'data').mkdir(exist_ok=True)
    with (root/'data'/'service.log').open('ab') as output:
        child = subprocess.Popen([sys.executable, str(root/'service.py')], cwd=root, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=output, start_new_session=True)
    (root/'data'/'service.pid').write_text(str(child.pid))
    print('Started local service. PID:', child.pid)
