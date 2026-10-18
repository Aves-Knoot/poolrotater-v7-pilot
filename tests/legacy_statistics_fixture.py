"""Exact accepted v7.1.10 counterparts for both rolling upgrade orders."""
import hashlib
import importlib.util
from pathlib import Path

BASE=Path(__file__).with_name('fixtures')/'v7110'
EXPECTED={'reader.py':'c7084201efa721e9cc35e4ad9bc4798bfb18942d21b064834217dd93c805a90c','server.py':'2f47cd57d856af2615551d7e11ee8a5db3970af2d077cac6d763b221f8b1c6b1','transport.py':'7dcc31ef75a3f44884df9f0d7e84edb40ef88d88d8bf5e41f67cf5c242862fc7','transport_worker.py':'e1f359d0b1a19a3cae9ca77c0531a5a61cd6a15f56c9499d7d7e4fc292106af9'}

def module(filename,namespace):
    path=BASE/filename
    if EXPECTED[filename]:assert hashlib.sha256(path.read_bytes()).hexdigest()==EXPECTED[filename]
    spec=importlib.util.spec_from_file_location(namespace,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value

LegacyHubServer=module('server.py','poolrotater_host.network._v7110_server').HubServer
LegacyReader=module('reader.py','poolrotater_host.activity._v7110_reader').Reader
LegacyTransport=module('transport.py','poolrotater_host.network._v7110_transport').Transport
