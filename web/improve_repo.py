"""
Generate repo-relative diffs of an operator's live edits for the self-improvement
flow.

The running neuron's source is reorganized inside the image (`neuron-lite/` lands
at `/Satori/Neuron`, `engine-lite/` at `/Satori/Engine`, `web/` at `/Satori/web`,
`skills/` at `/Satori/skills`). To propose a change upstream we need a unified
diff expressed in *repo* coordinates (`web/...`, `neuron-lite/...`) that applies
against a known commit.

This module produces exactly that, in pure Python, by diffing the live runtime
files against a **pristine baseline** shipped in the image at `/Satori/src` (repo
layout). The container->repo path mapping and the build commit live here — tested
code — so the external AI never has to translate paths or pick a base: it just
edits files and submits.

No git is required at runtime; the diff is `git apply`-compatible so the
maintainer side can apply it directly.
"""
import os
import difflib

# Runtime directory in the container -> its directory name in the repo.
RUNTIME_TO_REPO = {
    '/Satori/Neuron': 'neuron-lite',
    '/Satori/Engine': 'engine-lite',
    '/Satori/web': 'web',
    '/Satori/skills': 'skills',
}

# Pristine copy of the source (repo layout) shipped in the image.
BASELINE_DIR = os.environ.get('SATORI_BASELINE_DIR', '/Satori/src')

SKIP_DIR_NAMES = {'__pycache__', '.git', 'node_modules', '.pytest_cache', '.mypy_cache'}
SKIP_SUFFIXES = ('.pyc', '.pyo', '.so', '.log', '.joblib')


def build_sha():
    """The commit this image was built from, so diffs apply against the right base."""
    env = os.environ.get('SATORI_BUILD_SHA')
    if env and env.strip():
        return env.strip()
    for path in ('/Satori/BUILD_SHA', os.path.join(BASELINE_DIR, 'BUILD_SHA')):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                sha = f.read().strip()
                if sha:
                    return sha
        except OSError:
            continue
    return ''


def available():
    """True if a baseline is present (diff generation is possible)."""
    return os.path.isdir(BASELINE_DIR)


def container_to_repo(container_path):
    """Map an absolute container path to its repo-relative path, or None."""
    p = os.path.normpath(container_path)
    for runtime_dir, repo_dir in RUNTIME_TO_REPO.items():
        rd = os.path.normpath(runtime_dir)
        if p == rd:
            return repo_dir
        if p.startswith(rd + os.sep):
            rel = os.path.relpath(p, rd)
            return f'{repo_dir}/{rel}'.replace(os.sep, '/')
    return None


def _repo_to_runtime(repo_rel):
    head, _, rest = repo_rel.partition('/')
    for runtime_dir, repo_dir in RUNTIME_TO_REPO.items():
        if repo_dir == head:
            return os.path.join(runtime_dir, rest) if rest else runtime_dir
    return None


def _baseline_of(repo_rel):
    return os.path.join(BASELINE_DIR, repo_rel)


def _iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for fn in filenames:
            if fn.endswith(SKIP_SUFFIXES):
                continue
            yield os.path.join(dirpath, fn)


def _bytes_differ(a, b):
    try:
        with open(a, 'rb') as fa, open(b, 'rb') as fb:
            return fa.read() != fb.read()
    except OSError:
        return True


def _read_lines(path):
    """Text lines for diffing, or None if missing/binary."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def _status_of(repo_rel):
    base_exists = os.path.exists(_baseline_of(repo_rel))
    rt = _repo_to_runtime(repo_rel)
    rt_exists = bool(rt) and os.path.exists(rt)
    if not base_exists and rt_exists:
        return 'added'
    if base_exists and not rt_exists:
        return 'deleted'
    return 'modified'


def detect_changes():
    """All edits vs baseline, as a list of (repo_rel, status)."""
    changes = []
    for runtime_dir, repo_dir in RUNTIME_TO_REPO.items():
        if os.path.isdir(runtime_dir):
            for rt in _iter_files(runtime_dir):
                rel = os.path.relpath(rt, runtime_dir).replace(os.sep, '/')
                repo_rel = f'{repo_dir}/{rel}'
                base = _baseline_of(repo_rel)
                if not os.path.exists(base):
                    changes.append((repo_rel, 'added'))
                elif _bytes_differ(base, rt):
                    changes.append((repo_rel, 'modified'))
        base_root = _baseline_of(repo_dir)
        if os.path.isdir(base_root):
            for bf in _iter_files(base_root):
                rel = os.path.relpath(bf, base_root).replace(os.sep, '/')
                rt = os.path.join(runtime_dir, rel)
                if not os.path.exists(rt):
                    changes.append((f'{repo_dir}/{rel}', 'deleted'))
    return changes


def _file_block(repo_rel, status):
    base_lines = [] if status == 'added' else _read_lines(_baseline_of(repo_rel))
    cur_lines = [] if status == 'deleted' else _read_lines(_repo_to_runtime(repo_rel))
    if base_lines is None or cur_lines is None:
        return None  # binary or unreadable — skip
    fromfile = '/dev/null' if status == 'added' else f'a/{repo_rel}'
    tofile = '/dev/null' if status == 'deleted' else f'b/{repo_rel}'
    body = list(difflib.unified_diff(
        base_lines, cur_lines, fromfile=fromfile, tofile=tofile, lineterm=''))
    if not body:
        return None
    head = [f'diff --git a/{repo_rel} b/{repo_rel}']
    if status == 'added':
        head.append('new file mode 100644')
    elif status == 'deleted':
        head.append('deleted file mode 100644')
    return '\n'.join(head + body)


def generate(container_paths=None):
    """
    Build a git-apply-compatible, repo-relative unified diff of the live edits.

    container_paths: optional list of absolute container paths to scope the diff.
    If None, every changed file under the known source dirs is auto-detected.

    Returns (diff_text, changed_repo_paths). ('', []) if no baseline or no changes.
    """
    if not available():
        return '', []
    if container_paths:
        items = []
        for cp in container_paths:
            repo_rel = container_to_repo(cp)
            if repo_rel:
                items.append((repo_rel, _status_of(repo_rel)))
    else:
        items = detect_changes()

    blocks, changed = [], []
    for repo_rel, status in items:
        block = _file_block(repo_rel, status)
        if block:
            blocks.append(block)
            changed.append(repo_rel)
    diff = ('\n'.join(blocks) + '\n') if blocks else ''
    return diff, changed
