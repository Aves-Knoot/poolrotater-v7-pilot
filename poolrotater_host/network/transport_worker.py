"""Fixed private-stdin HTTPS worker, killed with its owning parent on Linux."""
import ctypes
import http.client
import json
import os
from pathlib import Path
import signal
import ssl
import sys
from urllib.parse import urlsplit


def main():
    parent=os.getppid()
    if ctypes.CDLL(None,use_errno=True).prctl(1,signal.SIGKILL,0,0,0)!=0:return 1
    if os.getppid()!=parent or parent==1:return 1
    raw=sys.stdin.buffer.read(786433)
    if len(raw)>786432:return 1
    value=json.loads(raw);url=urlsplit(value['origin'])
    maximum=512*1024 if value['path'].split('?',1)[0] in ('/v1/events/batches','/v1/ratings/state-query',
                         '/v1/ratings/snapshots/current','/v1/ratings/snapshot-page','/v1/activity/replica/current','/v1/activity/replica/page',
                         '/v2/activity/replica/current','/v2/activity/replica/page') else 65536
    if maximum==65536 and len(raw)>262144:return 1
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=value['ca_pem'])
    connection=http.client.HTTPSConnection(url.hostname,url.port or 443,timeout=value['timeout'],context=context)
    headers={'Accept':'application/json','Content-Type':'application/json','User-Agent':'PoolRotater-network/1'}
    if value['path'].startswith(('/v1/activity/replica/','/v2/activity/replica/')):
        headers['Accept-Encoding']='gzip'
    if value['token'] is not None:headers['Authorization']='Bearer '+value['token']
    body=None if value['body'] is None else json.dumps(value['body'],separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    try:
        connection.request(value['method'],value['path'],body=body,headers=headers);response=connection.getresponse()
        raw=response.read(maximum+1)
        if response.getheader('Content-Encoding')=='gzip':
            import zlib
            decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
            raw=decoder.decompress(raw,maximum+1)
            if len(raw)>maximum or not decoder.eof or decoder.unused_data:return 1
        elif response.getheader('Content-Encoding') not in (None,'identity'):return 1
        if len(raw)>maximum or response.getheader('Content-Type','').split(';',1)[0]!='application/json':return 1
        result={'status':response.status,'body_hex':raw.hex(),'retry_after':response.getheader('Retry-After')}
        sys.stdout.buffer.write(json.dumps(result,separators=(',',':')).encode());sys.stdout.buffer.flush()
    finally:connection.close()
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:raise SystemExit(1) from None
