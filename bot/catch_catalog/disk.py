"""Local import headroom; no collection or network dependencies."""
import shutil
from pathlib import Path
from .errors import DiskPressure
class DiskGuard:
    def __init__(self,path):self.path=Path(path)
    def check(self,extra=0):
        path=self.path
        while not path.exists():path=path.parent
        disk=shutil.disk_usage(path)
        if disk.free-extra<max(5*1024**3,disk.total*.20):
            raise DiskPressure('Catch research import would breach disk reserve')
