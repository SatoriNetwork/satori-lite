"""
Satori Neuron — self-improvement endpoints.

Exposes the machinery that lets an external AI (e.g. an operator's Claude)
discover what the neuron can do and propose improvements to it:

    GET  /api/skill           the self-improvement skill, as Markdown
    GET  /api/index           machine-readable index of every REST endpoint
    POST /api/improve/submit  hand a unified diff to the neuron so the
                              maintainers can turn it into an upstream PR

These endpoints are intentionally public and read-mostly: an external agent
must be able to fetch the skill and the index without an authenticated session.
They expose no wallet, vault, or key material.

The canonical skill text lives at ``skills/satori-self-improve/SKILL.md`` in the
repo (copied to ``/Satori/skills`` in the image) so it is also usable as a
drop-in slash skill and, later, as the payload for an MCP server.
"""
import os
import json
import time
import inspect
import logging
import tempfile

import requests
from flask import current_app, jsonify, request, Response

from web import improve_repo

logger = logging.getLogger(__name__)

REPO_URL = os.environ.get(
    'SATORI_REPO_URL', 'https://github.com/SatoriNetwork/satori-lite')
COMMUNITY_BRANCH = os.environ.get('SATORI_COMMUNITY_BRANCH', 'self-improve')

# Where the live source lives inside the running container (see Dockerfile).
SOURCE_PATHS = {
    'neuron': '/Satori/Neuron',          # neuron-lite/  (runtime, CLI, web wiring)
    'engine': '/Satori/Engine',          # engine-lite/  (forecasting engine)
    'web': '/Satori/web',                # web/          (UI + this REST API)
    'lib': '/Satori/Lib/satorilib',      # satorilib     (shared library)
}

# Embedded fallback so /api/skill never fails even if the file is missing.
_SKILL_FALLBACK = (
    "# Satori Neuron — Self-Improvement\n\n"
    "The full skill file could not be located on this neuron.\n\n"
    "1. `GET /api/index` — list every REST endpoint; answer the operator from "
    "the API when you can.\n"
    "2. If a code change is needed, edit the live container source "
    "(`/Satori/Neuron`, `/Satori/Engine`, `/Satori/web`) and restart the "
    "container for immediate relief.\n"
    f"3. Submit the change upstream as a PR to the `{COMMUNITY_BRANCH}` branch of "
    f"{REPO_URL}, or POST a unified diff to `/api/improve/submit`.\n"
)


def _skill_path():
    """Locate skills/satori-self-improve/SKILL.md in prod or dev layouts."""
    here = os.path.dirname(os.path.abspath(__file__))   # .../web
    root = os.path.dirname(here)                          # .../  (repo root or /Satori)
    candidates = [
        os.path.join(root, 'skills', 'satori-self-improve', 'SKILL.md'),
        '/Satori/skills/satori-self-improve/SKILL.md',
        os.path.join(here, 'skills', 'satori-self-improve', 'SKILL.md'),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _load_skill():
    path = _skill_path()
    if path:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except OSError as e:
            logger.warning('self_improve: could not read skill at %s: %s', path, e)
    return _SKILL_FALLBACK


def _categorize(path):
    """Group an endpoint path into a human-meaningful bucket."""
    table = [
        ('/api/skill', 'self-improve'),
        ('/api/index', 'self-improve'),
        ('/api/improve', 'self-improve'),
        ('/api/engine', 'engine'),
        ('/api/wallet', 'wallet'),
        ('/api/network', 'network'),
        ('/api/bounties', 'bounty'),
        ('/api/bounty', 'bounty'),
        ('/api/channels', 'channels'),
        ('/api/access', 'access'),
        ('/api/pool', 'pool'),
        ('/api/lender', 'lending'),
        ('/api/settings/relay', 'relay'),
        ('/api/relays', 'relay'),
        ('/api/nostr', 'nostr'),
        ('/api/settings', 'settings'),
        ('/api/system', 'system'),
        ('/api/balance', 'account'),
        ('/api/peer', 'account'),
    ]
    for prefix, name in table:
        if path.startswith(prefix):
            return name
    if path.startswith('/api/'):
        return 'api'
    return 'pages'


def _describe(endpoint):
    """First line of the view function's docstring, else a humanized name."""
    view = current_app.view_functions.get(endpoint)
    if view is not None:
        doc = inspect.getdoc(view)
        if doc:
            return doc.strip().splitlines()[0].strip()
    name = endpoint.split('.')[-1].replace('_', ' ').strip()
    return name[:1].upper() + name[1:] if name else endpoint


def build_index():
    """Build the structured index of every REST endpoint from the URL map."""
    endpoints = []
    for rule in current_app.url_map.iter_rules():
        if rule.endpoint == 'static':
            continue
        methods = sorted(rule.methods - {'HEAD', 'OPTIONS'})
        path = str(rule)
        endpoints.append({
            'path': path,
            'methods': methods,
            'name': rule.endpoint,
            'category': _categorize(path),
            'description': _describe(rule.endpoint),
        })
    endpoints.sort(key=lambda e: e['path'])

    categories = {}
    for e in endpoints:
        categories.setdefault(e['category'], []).append(e['path'])

    try:
        from satorineuron import VERSION
    except Exception:
        VERSION = 'unknown'

    return {
        'neuron': 'Satori Lite Neuron',
        'version': VERSION,
        'description': (
            'A node in the Satori decentralized prediction network. Consumes '
            'datastreams, trains models, publishes predictions. Runs in Docker '
            'with live Python source, so it can improve itself.'),
        'web_port': int(os.environ.get('SATORI_UI_PORT', os.environ.get('WEB_PORT', '24601'))),
        'skill_url': '/api/skill',
        'self_improve': {
            'enabled': True,
            'how': 'GET /api/skill for the full workflow.',
            'repo': REPO_URL,
            'community_branch': COMMUNITY_BRANCH,
            'submit_endpoint': '/api/improve/submit',
            'preview_endpoint': '/api/improve/diff',
            'source_paths_in_container': SOURCE_PATHS,
            # The neuron builds repo-relative diffs of your live edits itself —
            # no path translation needed — and records the base commit so they
            # apply cleanly upstream.
            'auto_diff': improve_repo.available(),
            'build_sha': improve_repo.build_sha(),
            'repos': improve_repo.repos(),
        },
        'endpoint_count': len(endpoints),
        'categories': categories,
        'endpoints': endpoints,
    }


def _index_as_markdown(index):
    lines = [
        f"# {index['neuron']} — API index ({index['version']})",
        '',
        index['description'],
        '',
        f"- Skill: `GET {index['skill_url']}`",
        f"- Repo: {index['self_improve']['repo']}",
        f"- Community branch: `{index['self_improve']['community_branch']}`",
        f"- Submit a diff: `POST {index['self_improve']['submit_endpoint']}`",
        f"- Endpoints: {index['endpoint_count']}",
        '',
        '| Method | Path | Category | Description |',
        '|--------|------|----------|-------------|',
    ]
    for e in index['endpoints']:
        methods = ', '.join(e['methods']) or 'GET'
        desc = e['description'].replace('|', '\\|')
        lines.append(f"| {methods} | `{e['path']}` | {e['category']} | {desc} |")
    return '\n'.join(lines) + '\n'


def _improvements_dir():
    """First writable location for queued improvement submissions."""
    candidates = [
        os.environ.get('SATORI_IMPROVE_DIR'),
        '/Satori/Neuron/data/improvements',
        '/data/improvements',
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'data', 'improvements'),
        os.path.join(tempfile.gettempdir(), 'satori-improvements'),
    ]
    for path in candidates:
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
            if os.access(path, os.W_OK):
                return path
        except OSError:
            continue
    return None


def _slug(text, limit=40):
    keep = [c.lower() if c.isalnum() else '-' for c in (text or '')]
    slug = ''.join(keep).strip('-')
    while '--' in slug:
        slug = slug.replace('--', '-')
    return slug[:limit] or 'improvement'


def _server():
    """The authenticated central-server client, or None if not connected."""
    try:
        from web.routes import get_startup
        startup = get_startup()
        return getattr(startup, 'server', None) if startup else None
    except Exception as e:
        logger.warning('self_improve: could not reach server client: %s', e)
        return None


def _forward_to_central(record, record_id):
    """Best-effort: forward a queued improvement to central, which opens the PR.

    Returns a dict merged into the submit response. Never raises — the local
    copy is always kept, so a forwarding failure does not lose the submission.
    """
    server = _server()
    if server is None:
        return {'forwarded': False, 'reason': 'no central connection'}
    payload = {
        'local_id': record_id,
        'title': record['title'],
        'description': record['description'],
        'diff': record['diff'],
        'repo': record.get('repo', 'satori-lite'),
        'base_sha': record.get('base_sha', ''),
        'branch': record.get('branch'),
        'files': record['files'],
        'group_id': record.get('group_id'),
        'group_size': record.get('group_size', 1),
        'group_repos': record.get('group_repos'),
    }
    try:
        resp = server._makeAuthenticatedCall(
            function=requests.post,
            endpoint='/api/v1/improve/submit',
            payload=json.dumps(payload),
            raiseForStatus=False)
    except Exception as e:
        logger.warning('self_improve: forward to central failed: %s', e)
        return {'forwarded': False, 'reason': str(e)}
    if resp is None:
        return {'forwarded': False, 'reason': 'no response from central'}
    if resp.status_code in (200, 201, 202):
        try:
            data = resp.json()
        except ValueError:
            data = {}
        central_id = data.get('id')
        return {
            'forwarded': True,
            'central_id': central_id,
            'status': data.get('status', 'queued'),
            'status_url': f'/api/improve/status/{central_id}' if central_id is not None else None,
        }
    return {'forwarded': False,
            'reason': f'central returned {resp.status_code}'}


def _proxy_central(method, endpoint, payload=None):
    """Proxy a wallet-authenticated call to central, returning a Flask response."""
    server = _server()
    if server is None:
        return jsonify({'ok': False, 'error': 'no central connection'}), 503
    try:
        resp = server._makeAuthenticatedCall(
            function=method, endpoint=endpoint,
            payload=(json.dumps(payload) if payload is not None else None),
            raiseForStatus=False)
    except Exception as e:
        logger.warning('self_improve: central proxy failed (%s): %s', endpoint, e)
        return jsonify({'ok': False, 'error': str(e)}), 502
    if resp is None:
        return jsonify({'ok': False, 'error': 'no response from central'}), 502
    return Response(resp.text, status=resp.status_code, mimetype='application/json')


def register_self_improve_routes(app):
    """Attach the self-improvement endpoints to the Flask app."""

    @app.after_request
    def _allow_cross_origin(response):
        # Scope CORS to the public self-improvement endpoints only.
        if (request.path in ('/api/skill', '/api/skill.md', '/api/index')
                or request.path.startswith('/api/improve/')):
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

    @app.route('/api/skill', methods=['GET'])
    @app.route('/api/skill.md', methods=['GET'])
    def get_self_improve_skill():
        """Self-improvement skill (Markdown) for an external AI to follow."""
        return Response(_load_skill(), mimetype='text/markdown')

    @app.route('/api/index', methods=['GET'])
    def get_api_index():
        """Machine-readable index of every REST endpoint on this neuron."""
        index = build_index()
        if request.args.get('format') in ('md', 'markdown', 'text'):
            return Response(_index_as_markdown(index), mimetype='text/markdown')
        return jsonify(index)

    @app.route('/api/improve/diff', methods=['POST', 'OPTIONS'])
    def preview_improvement_diff():
        """Preview the per-repo diffs the neuron will submit for your live edits."""
        if request.method == 'OPTIONS':
            return ('', 204)
        data = request.get_json(silent=True) or {}
        per_repo = improve_repo.generate(data.get('files'))
        return jsonify({
            'auto_diff': improve_repo.available(),
            'repos': per_repo,  # {repo: {diff, files, base_sha}}
        })

    @app.route('/api/improve/submit', methods=['POST', 'OPTIONS'])
    def submit_improvement():
        """Queue an improvement; the neuron opens one PR per affected repo.

        Pass just `title`/`description` and the neuron builds the diff(s) from
        your live edits — routing each to the correct repo (satori-lite for the
        neuron, satorilib for the shared library). Pass `files` (container paths)
        to scope it, or a ready-made unified `diff` (+ optional `repo`) yourself.
        """
        if request.method == 'OPTIONS':
            return ('', 204)
        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({'ok': False, 'error': "'title' is required."}), 400

        # One unit of work per repo: either an explicit diff, or auto-built diffs.
        if (data.get('diff') or '').strip():
            repo = data.get('repo') or 'satori-lite'
            units = {repo: {'diff': data['diff'],
                            'files': data.get('files') or [],
                            'base_sha': data.get('base_sha') or improve_repo.build_sha(repo)}}
        else:
            units = improve_repo.generate(data.get('files'))
            if not units:
                if improve_repo.available():
                    hint = ('Edit files under the source tree first, then '
                            "resubmit; or pass a unified 'diff'.")
                else:
                    hint = ('Auto-diff is unavailable on this neuron (no baseline '
                            "shipped); pass a unified 'diff' instead.")
                return jsonify({'ok': False,
                                'error': f'No changes detected. {hint}'}), 400

        out_dir = _improvements_dir()
        if out_dir is None:
            logger.error('self_improve: no writable improvements dir')
            return jsonify({
                'ok': False,
                'error': 'No writable location to store the submission on this neuron.',
            }), 500

        now = time.time()
        description = (data.get('description') or '').strip()
        author = data.get('author') or ''
        # One submit can span repos (e.g. neuron + satorilib). Tie the resulting
        # PRs together with a shared group id so they can be evaluated as a unit.
        group_id = data.get('group_id') or f"{int(now)}-{_slug(title)}"
        group_repos = list(units.keys())
        submissions = []
        for repo, unit in units.items():
            record = {
                'title': title,
                'description': description,
                'diff': unit['diff'],
                'repo': repo,
                'base_sha': unit.get('base_sha', ''),
                'branch': data.get('branch'),  # None -> central picks per-repo
                'author': author,
                'files': unit.get('files', []),
                'group_id': group_id,
                'group_size': len(units),
                'group_repos': group_repos,
                'received_at': now,
            }
            record_id = f"{int(now)}-{_slug(repo)}-{_slug(title)}"
            try:
                with open(os.path.join(out_dir, f"{record_id}.json"), 'w',
                          encoding='utf-8') as f:
                    json.dump(record, f, indent=2)
            except OSError as e:
                logger.error('self_improve: failed to store submission: %s', e)
                return jsonify({'ok': False,
                                'error': f'Failed to store submission: {e}'}), 500
            logger.info('self_improve: queued %s for repo=%s (%d bytes diff)',
                        record_id, repo, len(unit['diff']))
            entry = {'id': record_id, 'repo': repo, 'files': record['files']}
            entry.update(_forward_to_central(record, record_id))
            submissions.append(entry)

        return jsonify({
            'ok': True,
            'count': len(submissions),
            'group_id': group_id,
            'submissions': submissions,
            'message': ('Improvement queued. The neuron opens one PR per affected '
                        'repo after review; PRs in a group cross-reference each other.'),
        }), 202

    @app.route('/api/improve/open', methods=['GET'])
    def improvement_open():
        """Browse existing OPEN proposals (unmerged PRs) to reuse before building.
        Forwards `q`/`repo`/`limit` query params to central."""
        qs = request.query_string.decode()
        endpoint = '/api/v1/improve/open' + (f'?{qs}' if qs else '')
        return _proxy_central(requests.get, endpoint)

    @app.route('/api/improve/proposal/<proposal_id>', methods=['GET'])
    def improvement_proposal(proposal_id):
        """Full detail of an open proposal (incl. its diff) so the AI can evaluate it."""
        return _proxy_central(requests.get, f'/api/v1/improve/{proposal_id}')

    @app.route('/api/improve/adopt/<proposal_id>', methods=['POST', 'OPTIONS'])
    def improvement_adopt(proposal_id):
        """Record that this operator applied an open proposal locally (popularity)."""
        if request.method == 'OPTIONS':
            return ('', 204)
        return _proxy_central(requests.post, f'/api/v1/improve/{proposal_id}/adopt',
                              payload={})

    @app.route('/api/improve/status/<submission_id>', methods=['GET'])
    def improvement_status(submission_id):
        """Proxy the upstream PR status (state, PR url) of a forwarded improvement."""
        return _proxy_central(requests.get, f'/api/v1/improve/{submission_id}')
