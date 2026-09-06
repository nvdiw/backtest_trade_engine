"""Simple per-strategy paths, output ownership and process-level run locks."""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import os
from pathlib import Path
import re

_session = ContextVar('strategy_output_session', default=None)
OWNER_FILE = 'strategy_workspace.json'
LOCK_FILE = '.run.lock'


def strategy_slug(identifier):
    known = {'ma_strategy:ma_strategy': 'ma', 'pulse_strategy:pulse_strategy': 'pulse',
             'rsi_strategy:rsi_strategy': 'rsi',
             'example_strategy:example_strategy': 'example'}
    return known.get(identifier, re.sub(r'[^a-zA-Z0-9_.-]', '_', identifier))


def output_path(identifier, workflow):
    return Path('outputs') / strategy_slug(identifier) / workflow


def assert_strategy_path(path, identifier):
    """Reject a known foreign workspace, including files under its snapshots."""
    path = Path(path).resolve()
    for directory in (path, *path.parents):
        marker = directory / OWNER_FILE
        if marker.is_file():
            owner = json.loads(marker.read_text(encoding='utf-8'))['strategy']
            if owner != identifier:
                raise ValueError(f'{path} belongs to {owner}, not {identifier}; choose a separate path')


@contextmanager
def _run_lock(directory):
    # OS locks are released even after a crash; the harmless lock file remains.
    handle = (directory / LOCK_FILE).open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ValueError(f'Another run is using {directory}; choose another output directory') from exc
    try:
        yield
    finally:
        handle.close()


def claim_output(path, identifier, workflow):
    directory = Path(path).resolve()
    assert_strategy_path(directory, identifier)
    directory.mkdir(parents=True, exist_ok=True)
    stack = _session.get()
    if stack is None:
        raise RuntimeError('Output claiming requires an output_session')
    stack.enter_context(_run_lock(directory))
    # Recheck after acquiring the lock to prevent competing first-time owners.
    assert_strategy_path(directory, identifier)
    marker = directory / OWNER_FILE
    if marker.exists():
        owner = json.loads(marker.read_text(encoding='utf-8'))
        if owner['workflow'] != workflow:
            raise ValueError(f'{directory} contains {owner["workflow"]} outputs; use a separate {workflow} directory')
    else:
        # Adopt legacy optimizer directories only when their provenance agrees.
        manifest = directory / 'research_manifest.json'
        if manifest.is_file():
            owner = json.loads(manifest.read_text(encoding='utf-8')).get('strategy')
            if owner and owner != identifier:
                raise ValueError(f'{directory} belongs to {owner}; choose a separate output directory')
        marker.write_text(json.dumps({'strategy': identifier, 'workflow': workflow}, indent=2) + '\n',
                          encoding='utf-8')


def output_session(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with ExitStack() as stack:
            token = _session.set(stack)
            try:
                return function(*args, **kwargs)
            finally:
                _session.reset(token)
    return wrapped
