#!/usr/bin/env python3
"""Regenerate the English and Simplified Chinese reference catalogues.

The prototype's base strings live in L('English', '简体中文') calls. Other
languages are edited independently and are never overwritten by this script.
"""
import argparse
import json
import re
from pathlib import Path

root = Path(__file__).resolve().parent.parent
source = (root / 'dist/app.js').read_text(encoding='utf-8')
pairs = dict(re.findall(r"L\('((?:\\.|[^'])*)','((?:\\.|[^'])*)'\)", source))
if not pairs:
    raise SystemExit('No base translations found; check the extractor.')
parser = argparse.ArgumentParser()
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
for code, entries in [('en', {key: key for key in pairs}), ('zh-CN', pairs)]:
    path = root / 'dist/locales' / f'{code}.json'
    generated = json.dumps(entries, ensure_ascii=False, indent=2) + '\n'
    if args.check:
        if not path.exists() or path.read_text(encoding='utf-8') != generated:
            raise SystemExit(f'{path.name} is stale; run python3 scripts/sync-locales.py')
    else:
        path.write_text(generated, encoding='utf-8')
print(f'Base catalogues match {len(pairs)} interface strings.')
