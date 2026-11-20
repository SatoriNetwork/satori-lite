"""The launchers that create the neuron's container. The relay runs inside it
(strfry since 040e8dc), so a launcher hands it no Docker access, and removes
the relay's old sidecar containers, whose nginx would hold the relay port,
before its docker run."""
import os
import socket
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize('launcher, args, ui_port', [
    ('satori', ['1', 'restart'], 24601),
    ('satori-dev', ['restart'], 5001),
])
def test_launcher_removes_the_old_sidecars_and_mounts_no_docker_socket(
        tmp_path, launcher, args, ui_port):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'docker'
    fake.write_text('#!/bin/bash\necho "$*" >> "$DOCKER_LOG"\n')
    fake.chmod(0o755)
    host_socket = socket.socket(socket.AF_UNIX)
    host_socket.bind(str(tmp_path / 'docker.sock'))
    log = tmp_path / 'docker.log'
    env = {
        **{k: v for k, v in os.environ.items() if k != 'DOCKER_HOST'},
        'PATH': f'{bin_dir}:{os.environ["PATH"]}',
        'DOCKER_LOG': str(log),
        'SATORI_DOCKER_SOCKET': str(tmp_path / 'docker.sock'),
    }
    try:
        subprocess.run([str(REPO / launcher), *args], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=30, check=True)
    finally:
        host_socket.close()
    calls = log.read_text().splitlines()
    run = next(i for i, call in enumerate(calls) if call.startswith('run '))
    removed = [call.split()[2:] for call in calls[:run] if call.startswith('rm -f ')]
    for mode in ('public', 'private'):
        for part in ('nginx', 'strfry'):
            name = f'satori-local-relay-{ui_port}-{mode}-{part}'
            assert any(name in names for names in removed), (name, calls)
    assert 'docker.sock' not in calls[run]
    assert 'DOCKER_HOST' not in calls[run]
