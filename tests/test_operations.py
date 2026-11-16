import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from music_agent import state
from music_agent.config import DEFAULTS


def test_operation_lock_allows_worker_thread_persistence(tmp_path):
    from music_agent import operations

    config = replace(DEFAULTS, state_root=tmp_path / 'jobs')
    job = state.create(config, 'Worker', [state.Record(1, 'Artist', 'Song')])
    with operations.lock(config.state_root), ThreadPoolExecutor(max_workers=1) as pool:
        record = job.records()[0]
        record.genre = 'Techno'
        pool.submit(job.write_records, [record]).result(timeout=3)
    assert job.records()[0].genre == 'Techno'


def test_other_process_is_blocked_until_operation_releases(tmp_path):
    from music_agent import operations

    code = (
        'import sys\n'
        'from pathlib import Path\n'
        'from music_agent import operations, store\n'
        'try:\n'
        '    with operations.lock(Path(sys.argv[1])):\n'
        '        print("acquired")\n'
        'except store.WriterLocked:\n'
        '    print("blocked")\n'
    )
    with operations.lock(tmp_path):
        blocked = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                                 capture_output=True, text=True, timeout=10, check=True)
    released = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                              capture_output=True, text=True, timeout=10, check=True)
    assert blocked.stdout.strip() == 'blocked'
    assert released.stdout.strip() == 'acquired'
