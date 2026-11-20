"""The web UI is served over HTTPS with a certificate the neuron makes for
itself and keeps; plain HTTP on the same port is sent to the https:// address;
a connection that says nothing holds up no one else."""
import datetime
import http.client
import os
import socket
import ssl
import stat
import threading
import time
from unittest import mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID
from flask import Flask, request

from web import tls


def test_the_neuron_makes_a_certificate_and_keeps_it(tmp_path):
    cert_path, key_path = tls.certificate(str(tmp_path))
    assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
    with open(cert_path, 'rb') as f:
        cert = x509.load_pem_x509_certificate(f.read())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert 'localhost' in names.get_values_for_type(x509.DNSName)
    first = tls.fingerprint(cert_path)
    assert len(first.split(':')) == 32

    assert tls.certificate(str(tmp_path)) == (cert_path, key_path)
    assert tls.fingerprint(cert_path) == first  # the same one on the next start


@pytest.mark.parametrize('spoil', ['corrupt', 'other key', 'expired'])
def test_a_certificate_that_cannot_serve_is_made_anew(tmp_path, spoil):
    cert_path, key_path = tls.certificate(str(tmp_path))
    before = tls.fingerprint(cert_path)
    if spoil == 'corrupt':
        open(cert_path, 'w').write('not a certificate')
    elif spoil == 'other key':
        other = tmp_path / 'other'
        _, other_key = tls.certificate(str(other))
        os.replace(other_key, key_path)
    else:
        with open(key_path, 'rb') as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        now = datetime.datetime.now(datetime.timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Satori Neuron')])
        expired = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(1)
                   .not_valid_before(now - datetime.timedelta(days=30))
                   .not_valid_after(now - datetime.timedelta(days=1))
                   .sign(key, hashes.SHA256()))
        with open(cert_path, 'wb') as f:
            f.write(expired.public_bytes(serialization.Encoding.PEM))
        before = tls.fingerprint(cert_path)
    cert_path, key_path = tls.certificate(str(tmp_path))
    assert tls.fingerprint(cert_path) != before
    assert tls._usable(cert_path, key_path)


@pytest.fixture
def server(tmp_path):
    app = Flask(__name__)

    @app.route('/scheme')
    def scheme():
        return request.scheme

    cert_path, key_path = tls.certificate(str(tmp_path))
    server = tls.Server('127.0.0.1', 0, app, cert_path, key_path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, cert_path
    server.shutdown()


def _https_get(port, cert_path, path='/scheme', timeout=5):
    context = ssl.create_default_context(cafile=cert_path)
    conn = http.client.HTTPSConnection('localhost', port, context=context, timeout=timeout)
    conn.request('GET', path)
    r = conn.getresponse()
    return r.status, r.read().decode()


def test_the_page_is_served_over_https_with_that_certificate(server):
    server, cert_path = server
    assert _https_get(server.port, cert_path) == (200, 'https')


def test_plain_http_is_sent_to_the_https_address(server):
    server, _ = server
    conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=5)
    conn.request('GET', '/dashboard?tab=wallet')
    r = conn.getresponse()
    assert r.status == 307
    assert r.getheader('Location') == f'https://127.0.0.1:{server.port}/dashboard?tab=wallet'


def _raw(port, head: bytes) -> bytes:
    with socket.create_connection(('127.0.0.1', port), timeout=5) as s:
        s.sendall(head)
        return s.recv(4096)


def test_plain_http_to_a_host_without_a_port_goes_to_this_port(server):
    server, _ = server
    answer = _raw(server.port, b'GET / HTTP/1.1\r\nHost: example.com\r\n\r\n')
    assert f'Location: https://example.com:{server.port}/\r\n'.encode() in answer


@pytest.mark.parametrize('head', [
    b'GET / HTTP/1.1\r\n\r\n',
    b'GET / HTTP/1.1\r\nHost: a b\r\n\r\n',
    b'GET / HTTP/1.1\r\nHost: a"><script>\r\n\r\n',
])
def test_plain_http_without_a_good_host_is_not_redirected(server, head):
    server, _ = server
    answer = _raw(server.port, head)
    assert answer.startswith(b'HTTP/1.1 400') and b'Location' not in answer


def test_a_silent_connection_holds_up_no_one(server):
    server, cert_path = server
    silent = socket.create_connection(('127.0.0.1', server.port))
    try:
        started = time.monotonic()
        assert _https_get(server.port, cert_path, timeout=3) == (200, 'https')
        assert time.monotonic() - started < 2
    finally:
        silent.close()


def test_the_remote_access_page_needs_no_login_and_fits_this_neuron():
    from web.app import create_app
    client = create_app(testing=True).test_client()
    page = client.get('/remote-access', base_url='https://203.0.113.5:24603').get_data(as_text=True)
    assert 'ssh -N -L 24603:localhost:24603 you@203.0.113.5' in page
    assert 'openssl x509 -in ./3/config/ui-cert.pem' in page
    assert 'docker logs satori-3' in page
    import web.routes as routes
    with mock.patch.object(routes, 'check_wallet_file_exists', return_value=True):
        login = client.get('/login', base_url='https://203.0.113.5:24603')
    assert login.status_code == 200
    assert 'href="/remote-access"' in login.get_data(as_text=True)

