"""30-minute native baseline/candidate CPU and RSS observation."""
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from monitor.database import Database

OUT = ROOT / 'data' / 'perf-review'
BASE = OUT / 'baseline'
env = dict(os.environ)
env.pop('QT_QPA_PLATFORM', None)
results = []
for scenario in ('idle', 'tracking', 'window'):
    for version, source in (('baseline', BASE), ('optimized', ROOT)):
        name = f'{scenario}-{version}'
        database = OUT / f'{name}-{int(time.time())}.db'
        db = Database(str(database))
        if scenario != 'idle':
            db.add_program(str(Path(os.environ['WINDIR']) / 'explorer.exe'), 'Explorer')
        db.set_setting('theme', 'dark')
        db.close()
        log = open(OUT / f'{name}.log', 'w', encoding='utf-8')
        args = [sys.executable, str(source / 'main.py'), '--db', str(database),
                '--windowed' if scenario == 'window' else '--silent', '--quit-after', '300']
        child = subprocess.Popen(args, cwd=source, env=env, stdout=log, stderr=log,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        proc = psutil.Process(child.pid)
        started = time.monotonic()
        samples = []
        print(f'START {name}', flush=True)
        while child.poll() is None and time.monotonic() - started < 330:
            try:
                cpu = proc.cpu_times()
                io = proc.io_counters()
                samples.append(dict(t=time.monotonic()-started, cpu=cpu.user+cpu.system,
                                    rss=proc.memory_info().rss, writes=io.write_bytes))
            except psutil.NoSuchProcess:
                break
            time.sleep(5)
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=15)
        log.close()
        stable = [s for s in samples if s['t'] >= 20]
        result = dict(scenario=scenario, version=version, exit_code=child.returncode,
                      samples=samples, database=str(database))
        if len(stable) >= 2:
            first, last = stable[0], stable[-1]
            result.update(cpu_seconds=last['cpu']-first['cpu'],
                          elapsed=last['t']-first['t'],
                          cpu_one_core_percent=100*(last['cpu']-first['cpu'])/(last['t']-first['t']),
                          median_rss_mb=statistics.median(s['rss'] for s in stable)/1048576,
                          rss_change_mb=(last['rss']-first['rss'])/1048576,
                          write_bytes=last['writes']-first['writes'])
        results.append(result)
        (OUT / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        print('DONE '+json.dumps({k:v for k,v in result.items() if k!='samples'}), flush=True)
        if child.returncode != 0 or len(stable) < 40:
            raise SystemExit('Aborted: invalid observation run; inspect log')
print('ALL DONE', flush=True)
