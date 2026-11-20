"""The web UI over HTTPS, with a certificate the neuron makes for itself.

The neuron makes a self-signed certificate on its first start and keeps it in
its config folder, so it lasts through restarts and image updates. A browser
warns about it once; the operator checks its fingerprint (the neuron logs it
at start; /remote-access says how) and goes on. From then on the passphrase,
the session and the keys cross the network encrypted.
"""
import datetime
import hashlib
import ipaddress
import os
import re
import socket
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from werkzeug.serving import ThreadedWSGIServer

CERT_FILE = 'ui-cert.pem'
KEY_FILE = 'ui-key.pem'
HANDSHAKE_SECONDS = 10

_DER = serialization.Encoding.DER
_SPKI = serialization.PublicFormat.SubjectPublicKeyInfo


def certificate(folder: str) -> tuple[str, str]:
    """The paths of the UI's certificate and key in the folder, made anew
    when either is missing, unreadable, mismatched or expired."""
    cert_path = os.path.join(folder, CERT_FILE)
    key_path = os.path.join(folder, KEY_FILE)
    if not _usable(cert_path, key_path):
        _make(cert_path, key_path)
    return cert_path, key_path


def fingerprint(cert_path: str) -> str:
    """The certificate's SHA-256 fingerprint as browsers and openssl show it."""
    with open(cert_path, 'rb') as f:
        der = x509.load_pem_x509_certificate(f.read()).public_bytes(_DER)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ':'.join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _usable(cert_path: str, key_path: str) -> bool:
    try:
        with open(cert_path, 'rb') as f:
            cert = x509.load_pem_x509_certificate(f.read())
        with open(key_path, 'rb') as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
    except (OSError, ValueError, TypeError):
        return False
    tomorrow = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    return (cert.not_valid_after_utc > tomorrow
            and cert.public_key().public_bytes(_DER, _SPKI)
            == key.public_key().public_bytes(_DER, _SPKI))


def _make(cert_path: str, key_path: str):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Satori Neuron')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName('localhost'),
            x509.IPAddress(ipaddress.ip_address('127.0.0.1')),
            x509.IPAddress(ipaddress.ip_address('::1'))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256()))
    os.makedirs(os.path.dirname(key_path) or '.', exist_ok=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
    os.chmod(key_path, 0o600)  # in case the file was already there
    with open(cert_path, 'wb') as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


class Server(ThreadedWSGIServer):
    """Werkzeug's threaded server, shaking hands for TLS in each connection's
    own thread. (Werkzeug's own TLS shakes hands in the loop that accepts
    connections, where one connection that says nothing, a port scanner's,
    holds up all the others.) A connection that opens with plain HTTP is told
    the https:// address."""

    def __init__(self, host: str, port: int, app, cert_path: str, key_path: str):
        super().__init__(host, port, app)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert_path, key_path)
        self.ssl_context = context  # werkzeug then tells the app it is https

    def finish_request(self, request, client_address):
        request.settimeout(HANDSHAKE_SECONDS)
        try:
            first = request.recv(1, socket.MSG_PEEK)
            if first != b'\x16':  # the first byte of a TLS handshake
                if first:
                    _send_to_https(request, self.port)
                return
            conn = self.ssl_context.wrap_socket(request, server_side=True)
        except OSError:  # gone, silent too long, or a handshake that failed
            return
        try:
            conn.settimeout(None)
            super().finish_request(conn, client_address)
        finally:
            conn.close()


_HOST = re.compile(r'(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?')
_PATH = re.compile(r'/(?!/)[\x21-\x7e]*')


def _send_to_https(conn, port: int):
    """Answer a plain-HTTP request with its https:// address."""
    head = b''
    while b'\r\n\r\n' not in head and len(head) < 16384:
        chunk = conn.recv(4096)
        if not chunk:
            break
        head += chunk
    lines = head.split(b'\r\n')
    words = lines[0].split(b' ')
    path = words[1].decode('latin-1') if len(words) == 3 else '/'
    path = path if _PATH.fullmatch(path) else '/'
    host = next((line[5:].strip().decode('latin-1') for line in lines[1:]
                 if line[:5].lower() == b'host:'), '')
    if _HOST.fullmatch(host):
        if not re.search(r':\d+$', host):
            host = f'{host}:{port}'
        location = f'https://{host}{path}'
        status = '307 Temporary Redirect'
        extra = f'Location: {location}\r\n'
        body = f'This page is at {location}\n'
    else:
        status, extra = '400 Bad Request', ''
        body = f'This page is served over HTTPS: https://<address>:{port}/\n'
    body = body.encode('latin-1')
    conn.sendall((
        f'HTTP/1.1 {status}\r\n{extra}Content-Type: text/plain\r\n'
        f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n'
    ).encode('latin-1') + body)
