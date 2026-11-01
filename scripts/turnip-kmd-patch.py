#!/usr/bin/env python3
"""Edit the new-file hunk of turnip-darwin-kmd.patch as a real file: `extract OUT.cc` and `inject IN.cc` (rewrites the hunk header)."""
import re, sys
from pathlib import Path
PATCH = str(Path(__file__).resolve().parents[1] / 'patches/mesa/turnip-darwin-kmd.patch')
HEAD = 'diff --git a/src/freedreno/vulkan/tu_knl_darwin.cc b/src/freedreno/vulkan/tu_knl_darwin.cc\n'
p = open(PATCH).read()
a = p.index(HEAD)
m = re.search(r'^@@ -0,0 \+1,(\d+) @@\n', p[a:], re.M)
start = a + m.end()
n = int(m.group(1))
lines = p[start:].split('\n')
body = lines[:n]
assert all(l.startswith('+') for l in body)
rest_start = start + sum(len(l) + 1 for l in body)
if sys.argv[1] == 'extract':
    open(sys.argv[2], 'w').write('\n'.join(l[1:] for l in body) + '\n')
else:
    text = open(sys.argv[2]).read().rstrip('\n').split('\n')
    new = ''.join('+' + l + '\n' for l in text)
    out = p[:a + m.start()] + '@@ -0,0 +1,%d @@\n' % len(text) + new + p[rest_start:]
    open(PATCH, 'w').write(out)
