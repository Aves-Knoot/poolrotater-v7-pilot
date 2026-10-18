"""Shared transfer CPU budget. Native room workers never join this slice."""
import os
from pathlib import Path
import stat
import tempfile
from ..provisioning import protected
from .contracts import require

NAME='poolrotater-v7-transfer.slice'
TEXT='''[Unit]
Description=PoolRotater shared background transfer CPU budget

[Slice]
CPUAccounting=yes
CPUQuota=50%
'''


def ensure(base):
    """Explicit root network-unit administration only; never a service worker."""
    require(os.geteuid()==0,'transfer_policy_administrator_required')
    base=protected(Path(base));path=base/NAME
    if os.path.lexists(path):
        protected(path);info=path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<4096
                and path.read_bytes()==TEXT.encode(),'transfer_slice_requires_review')
        return False
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=base,prefix='.prv7-transfer-',delete=False) as stream:
            temporary=Path(stream.name);os.fchmod(stream.fileno(),0o644)
            stream.write(TEXT.encode());stream.flush();os.fsync(stream.fileno())
        os.link(temporary,path)
        fd=os.open(base,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
    return True
