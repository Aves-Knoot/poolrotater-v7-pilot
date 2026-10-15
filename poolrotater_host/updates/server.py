"""Separate read-only executable-release origin; signing custody stays private."""
import re

from ..catalog_server import CatalogServer
from .contracts import MAX_ARTIFACT, MAX_METADATA, require


def route(value):
    require(type(value) is str and len(value) <= 160, 'update_route_refused')
    if value == '/metadata/release-root.json' or re.fullmatch(
            r'/metadata/([1-9][0-9]{0,15}\.)?releases\.json', value):
        return value[1:], MAX_METADATA
    if re.fullmatch(r'/targets/[a-f0-9]{64}\.runtime\.zip', value):
        return value[1:], MAX_ARTIFACT
    require(False, 'update_route_refused')


class ReleaseServer(CatalogServer):
    route = staticmethod(route)

    def cache_control(self,name):
        return 'no-cache'if name in('metadata/releases.json','metadata/release-root.json')else'public, max-age=86400, immutable'
