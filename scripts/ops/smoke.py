"""Read-only HTTP smoke checks. Never joins, approves, resets, or follows redirects."""
import argparse
import json
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('origin')
    parser.add_argument('--allow-local-http',action='store_true')
    args=parser.parse_args(argv)
    try:
        origin=urlsplit(args.origin)
        allowed=origin.scheme=='https' or (args.allow_local_http and origin.scheme=='http' and origin.hostname in ('localhost','127.0.0.1','::1'))
        if not allowed or not origin.hostname or origin.username or origin.password or origin.path not in ('','/') or origin.query or origin.fragment:
            raise ValueError()
        origin.port  # validate before requesting; never echo supplied values
    except ValueError:
        parser.error('Supply an HTTPS origin without credentials, path, or query. Local HTTP requires --allow-local-http.')
    opener=urllib.request.build_opener(NoRedirect())
    healthy=True
    for path in ('/api/live','/api/ready'):
        try:
            with opener.open(origin.geturl().rstrip('/')+path,timeout=8) as response:
                payload=json.loads(response.read(16384))
                ok=response.status==200 and payload.get('status')=='ok'
                trace=bool(response.headers.get('X-Request-ID'))
                healthy=healthy and ok and trace
                print(json.dumps({'endpoint':path,'ok':ok,'trace_header':trace}))
        except Exception as error:
            healthy=False
            print(json.dumps({'endpoint':path,'ok':False,'error_type':type(error).__name__}))
    return 0 if healthy else 1


if __name__=='__main__':
    sys.exit(main())
