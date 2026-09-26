"""Start the SQLite support service for the user's existing local n8n."""
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
CONFIG = json.loads((ROOT / 'config.json').read_text())


def healthy(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def main():
    service_url = f"http://127.0.0.1:{CONFIG['port']}/health"
    if not healthy(service_url):
        with (DATA / 'service.log').open('ab') as output:
            process = subprocess.Popen([sys.executable, str(ROOT / 'service.py')], cwd=ROOT,
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                       start_new_session=True)
        (DATA / 'service.pid').write_text(f'{process.pid}\n')
        for _ in range(30):
            if healthy(service_url):
                break
            if process.poll() is not None:
                raise RuntimeError('Support service exited. Check data/service.log')
            time.sleep(1)
        else:
            raise RuntimeError('Support service did not become ready. Check data/service.log')
    print(f"Dashboard: http://localhost:{CONFIG['port']}")

    n8n_url = CONFIG['n8n_base_url'].rstrip('/')
    if healthy(n8n_url + '/healthz'):
        print(f'Existing n8n is reachable: {n8n_url}')
    else:
        print(f'Existing n8n is offline at {n8n_url}. Start your n8n instance to send events.')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(f'Could not start Food Delivery Control: {error}')
