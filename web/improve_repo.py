"""
Generate repo-relative diffs of an operator's live edits for self-improvement.

The neuron container holds source from **two** repos, reorganized:

  /Satori/Neuron        <- satori-lite : neuron-lite/
  /Satori/Engine        <- satori-lite : engine-lite/
  /Satori/web           <- satori-lite : web/
  /Satori/skills        <- satori-lite : skills/
  /Satori/Lib/satorilib <- satorilib   : src/satorilib/

An edit must become a pull request to the **right** repository. This module
diffs the live runtime files against pristine baselines shipped in the image,
groups the changes by repo, and emits one `git apply`-compatible, repo-relative
diff per repo (each with that repo's build commit as the base). The external AI
never has to translate paths or pick a repo — it just edits files and submits;
central routes each diff to its repo.
"""
import os
import difflib
from typing import Any, Dict, List, Optional

# Baseline roots (overridable for tests / non-default layouts).
SATORI_BASELINE = os.environ.get("SATORI_BASELINE_DIR", "/Satori/src")
SATORILIB_BASELINE = os.environ.get(
    "SATORILIB_BASELINE_DIR", "/Satori/src-lib/src/satorilib")

# container runtime dir -> {repo, repo-relative prefix, pristine baseline dir}
SOURCES: List[Dict[str, str]] = [
    {"runtime": "/Satori/Neuron", "repo": "satori-lite", "prefix": "neuron-lite",
     "baseline": os.path.join(SATORI_BASELINE, "neuron-lite")},
    {"runtime": "/Satori/Engine", "repo": "satori-lite", "prefix": "engine-lite",
     "baseline": os.path.join(SATORI_BASELINE, "engine-lite")},
    {"runtime": "/Satori/web", "repo": "satori-lite", "prefix": "web",
     "baseline": os.path.join(SATORI_BASELINE, "web")},
    {"runtime": "/Satori/skills", "repo": "satori-lite", "prefix": "skills",
     "baseline": os.path.join(SATORI_BASELINE, "skills")},
    {"runtime": "/Satori/Lib/satorilib", "repo": "satorilib", "prefix": "src/satorilib",
     "baseline": SATORILIB_BASELINE},
]

SKIP_DIR_NAMES = {"__pycache__", ".git", "node_modules", ".pytest_cache", ".mypy_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".so", ".log", ".joblib")

# repo -> (env var, [file fallbacks]) for the build commit the diff applies against
_BUILD_SHA = {
    "satori-lite": ("SATORI_BUILD_SHA", ["/Satori/BUILD_SHA"]),
    "satorilib": ("SATORILIB_BUILD_SHA", ["/Satori/SATORILIB_BUILD_SHA"]),
}


def build_sha(repo: str = "satori-lite") -> str:
    """The commit `repo`'s image was built from, so its diff applies cleanly."""
    env_key, files = _BUILD_SHA.get(repo, (None, []))
    if env_key:
        v = os.environ.get(env_key)
        if v and v.strip():
            return v.strip()
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                sha = f.read().strip()
                if sha:
                    return sha
        except OSError:
            continue
    return ""


def available() -> bool:
    """True if at least one baseline is present (diff generation possible)."""
    return any(os.path.isdir(s["baseline"]) for s in SOURCES)


def repos() -> List[str]:
    """Distinct repos this neuron can propose changes to."""
    seen = []
    for s in SOURCES:
        if s["repo"] not in seen:
            seen.append(s["repo"])
    return seen


def _source_for(container_path: str) -> Optional[Dict[str, str]]:
    p = os.path.normpath(container_path)
    for s in SOURCES:
        rd = os.path.normpath(s["runtime"])
        if p == rd or p.startswith(rd + os.sep):
            return s
    return None


def container_to_repo(container_path: str):
    """(repo, repo-relative path) for an absolute container path, or (None, None)."""
    s = _source_for(container_path)
    if not s:
        return None, None
    p, rd = os.path.normpath(container_path), os.path.normpath(s["runtime"])
    if p == rd:
        return s["repo"], s["prefix"]
    rel = os.path.relpath(p, rd).replace(os.sep, "/")
    return s["repo"], f'{s["prefix"]}/{rel}'


def _iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for fn in filenames:
            if not fn.endswith(SKIP_SUFFIXES):
                yield os.path.join(dirpath, fn)


def _bytes_differ(a, b):
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() != fb.read()
    except OSError:
        return True


def _read_lines(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def _status(baseline_file, runtime_file):
    be, re_ = os.path.exists(baseline_file), os.path.exists(runtime_file)
    if not be and re_:
        return "added"
    if be and not re_:
        return "deleted"
    return "modified"


def _file_block(repo_rel, status, baseline_file, runtime_file):
    base_lines = [] if status == "added" else _read_lines(baseline_file)
    cur_lines = [] if status == "deleted" else _read_lines(runtime_file)
    if base_lines is None or cur_lines is None:
        return None  # binary / unreadable — skip
    fromfile = "/dev/null" if status == "added" else f"a/{repo_rel}"
    tofile = "/dev/null" if status == "deleted" else f"b/{repo_rel}"
    body = list(difflib.unified_diff(
        base_lines, cur_lines, fromfile=fromfile, tofile=tofile, lineterm=""))
    if not body:
        return None
    head = [f"diff --git a/{repo_rel} b/{repo_rel}"]
    if status == "added":
        head.append("new file mode 100644")
    elif status == "deleted":
        head.append("deleted file mode 100644")
    return "\n".join(head + body)


def _changes_for_source(s):
    out = []
    runtime, baseline, prefix = s["runtime"], s["baseline"], s["prefix"]
    if os.path.isdir(runtime):
        for rt in _iter_files(runtime):
            rel = os.path.relpath(rt, runtime).replace(os.sep, "/")
            bf = os.path.join(baseline, rel)
            if not os.path.exists(bf):
                out.append((f"{prefix}/{rel}", "added", bf, rt))
            elif _bytes_differ(bf, rt):
                out.append((f"{prefix}/{rel}", "modified", bf, rt))
    if os.path.isdir(baseline):
        for bf in _iter_files(baseline):
            rel = os.path.relpath(bf, baseline).replace(os.sep, "/")
            rt = os.path.join(runtime, rel)
            if not os.path.exists(rt):
                out.append((f"{prefix}/{rel}", "deleted", bf, rt))
    return out


def generate(container_paths: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """Build per-repo diffs of the live edits.

    container_paths: optional absolute container paths to scope to; if None,
    auto-detect every changed file under the known source dirs.

    Returns {repo: {"diff": str, "files": [repo_rel, ...], "base_sha": str}} —
    one entry per repo that has changes (empty dict if none / no baseline).
    """
    by_repo: Dict[str, list] = {}
    if container_paths:
        for cp in container_paths:
            s = _source_for(cp)
            if not s:
                continue
            p, rd = os.path.normpath(cp), os.path.normpath(s["runtime"])
            if p == rd:
                continue
            rel = os.path.relpath(p, rd).replace(os.sep, "/")
            bf = os.path.join(s["baseline"], rel)
            by_repo.setdefault(s["repo"], []).append(
                (f'{s["prefix"]}/{rel}', _status(bf, p), bf, p))
    else:
        for s in SOURCES:
            ch = _changes_for_source(s)
            if ch:
                by_repo.setdefault(s["repo"], []).extend(ch)

    result: Dict[str, Dict[str, Any]] = {}
    for repo, items in by_repo.items():
        blocks, files = [], []
        for repo_rel, status, bf, rt in items:
            block = _file_block(repo_rel, status, bf, rt)
            if block:
                blocks.append(block)
                files.append(repo_rel)
        if blocks:
            result[repo] = {"diff": "\n".join(blocks) + "\n",
                            "files": files, "base_sha": build_sha(repo)}
    return result
