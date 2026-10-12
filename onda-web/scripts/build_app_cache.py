"""Fingerprint only public UI assets; never include APIs, sessions or media."""
from pathlib import Path
from html.parser import HTMLParser
import ast
import hashlib
import json
import re

class AssetParser(HTMLParser):
    def __init__(self):super().__init__();self.paths=set()
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='script' and attrs.get('src'):self.paths.add(attrs['src'])
        if tag=='link' and attrs.get('rel') in ('stylesheet','preload','icon','manifest','apple-touch-icon'):self.paths.add(attrs['href'])

def build(root):
    public=(root/'public').resolve()
    parser=AssetParser();parser.feed((public/'index.html').read_text())
    paths=parser.paths | {'/index.html','/offline.html','/offline.js','/assets/onda-logo.webp','/assets/onda-app-icon.png'}
    declaration=re.search(r'const studioScripts\s*=\s*(\[[^;]+\]);',(public/'account.js').read_text())
    if not declaration:raise ValueError('Studio asset declaration unavailable')
    paths.update(ast.literal_eval(declaration[1]))
    digests={}
    for path in sorted(paths):
        file=(public/path.removeprefix('/')).resolve()
        if not path.startswith('/') or '..' in path or '?' in path or public not in file.parents or file.suffix not in {'.html','.js','.css','.woff2','.png','.webp','.webmanifest'}:raise ValueError('Not a public UI asset')
        if file.stat().st_size>4*1024**2:raise ValueError('UI cache asset too large')
        digests[path]=hashlib.sha256(file.read_bytes()).hexdigest()
    worker=public/'app-worker.js';source=worker.read_text()
    pattern=r'// BEGIN GENERATED SHELL[\s\S]*?// END GENERATED SHELL'
    template=re.sub(pattern,'// GENERATED SHELL',source)
    version=hashlib.sha256((template+json.dumps(digests,sort_keys=True)).encode()).hexdigest()[:24]
    generated=f'// BEGIN GENERATED SHELL\nconst CACHE_VERSION = {json.dumps(version)};\nconst SHELL = {json.dumps(digests,indent=2)};\n// END GENERATED SHELL'
    result,count=re.subn(pattern,lambda _:generated,source)
    if count!=1:raise ValueError('Worker cache marker unavailable')
    worker.write_text(result)
    return version,len(paths),sum((public/p.removeprefix('/')).stat().st_size for p in paths)

def main():
    version,count,size=build(Path(__file__).resolve().parents[1])
    print(f'Public app cache: {count} assets, {size} bytes, version {version}')

if __name__=='__main__':main()
