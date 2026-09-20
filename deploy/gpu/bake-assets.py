"""Build-time public cache with exact sizes/hashes; never run model code."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


def bake(manifest_path, destination):
    manifest = json.loads(Path(manifest_path).read_bytes())
    if manifest.get('schema_version') != 1 or not 1 <= len(manifest.get('assets', [])) <= 128:
        raise ValueError('invalid public asset manifest')
    total = sum(a['bytes'] for a in manifest['assets'])
    if not 0 < total <= 12_000_000_000:
        raise ValueError('public cache is too large')
    root = Path(destination)
    root.mkdir(mode=0o755)
    until = time.monotonic() + 1200
    client = build_opener(ProxyHandler({}))
    for asset in manifest['assets']:
        name = asset['path']
        if (not isinstance(name, str) or not re.fullmatch(r'(models|datasets)/[A-Za-z0-9_./-]+', name)
                or any(p in ('', '.', '..') for p in name.split('/'))
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', asset['sha256'])):
            raise ValueError('invalid public cache member')
        target = root / name
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        source = None
        if 'url' in asset:
            url = urlsplit(asset['url'])
            if url.scheme != 'https' or url.hostname != 'huggingface.co' or url.username or url.password or url.query or url.fragment:
                raise ValueError('only frozen public Hugging Face assets are permitted')
            source = client.open(Request(asset['url'], headers={'User-Agent': 'Probe-public-cache/1'}), timeout=30)
        digest = hashlib.sha256()
        count = 0
        try:
            with target.open('xb') as output:
                while True:
                    if time.monotonic() >= until:
                        raise TimeoutError('public cache build deadline')
                    block = source.read(1024*1024) if source else base64.b64decode(asset['inline_base64'], validate=True)
                    if not block:
                        break
                    count += len(block)
                    if count > asset['bytes']:
                        raise ValueError('public cache byte limit')
                    digest.update(block)
                    output.write(block)
                    if source is None:
                        break
                output.flush()
                os.fsync(output.fileno())
            if count != asset['bytes'] or 'sha256:'+digest.hexdigest() != asset['sha256']:
                raise ValueError('public cache hash or size mismatch')
            target.chmod(0o444)
        finally:
            if source:
                source.close()
        print(json.dumps({'asset': name, 'bytes': count, 'hash_verified': True}), flush=True)


if __name__ == '__main__':
    try:
        bake(sys.argv[1], sys.argv[2])
    except Exception as error:
        print(json.dumps({'public_cache_build_failed': type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
