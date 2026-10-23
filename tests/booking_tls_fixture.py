"""Invented TLS material for offline and donor booking-system qualification."""
import os
from pathlib import Path
import subprocess
from poolrotater_host.oauth import AuthError

def certificates(folder):
    if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1': raise AuthError('offline_fixture_required')
    folder = Path(folder)
    def run(*args):
        subprocess.run(['/usr/bin/openssl', *args], cwd=folder, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    run('req', '-config', '/dev/null', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
        '-subj', '/CN=Invented offline CA', '-keyout', 'ca.key', '-out', 'ca.crt',
        '-addext', 'basicConstraints=critical,CA:TRUE')
    for name, san in (('server', 'IP:127.0.0.1'), ('wrong-host', 'DNS:localhost')):
        run('req', '-config', '/dev/null', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=Invented offline provider',
            '-keyout', name+'.key', '-out', name+'.csr')
        (folder/(name+'.ext')).write_text('subjectAltName='+san+'\nextendedKeyUsage=serverAuth\n')
        run('x509', '-req', '-in', name+'.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key',
            '-CAcreateserial', '-days', '2', '-extfile', name+'.ext', '-out', name+'.crt')
    for path in folder.iterdir(): path.chmod(0o600)

