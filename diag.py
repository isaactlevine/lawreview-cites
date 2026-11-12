import csv, io, itertools, re, subprocess, sys, collections
import lawrev_cites as L
csv.field_size_limit(2**31 - 1)
url = L.discover()["opinions"][0]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 300000
def stream():
    p1 = subprocess.Popen(["curl", "-sSL", url], stdout=subprocess.PIPE)
    p2 = subprocess.Popen(L.decompressor(), stdin=p1.stdout, stdout=subprocess.PIPE)
    return (p1, p2), io.TextIOWrapper(p2.stdout, encoding="utf-8", errors="replace", newline="")
for name, kw in [("escape", dict(quotechar='"', doublequote=False, escapechar="\\")),
                 ("doubled", dict(quotechar='"', doublequote=True)),
                 ("both", dict(quotechar='"', doublequote=True, escapechar="\\"))]:
    procs, f = stream()
    rd = csv.reader((l.replace("\x00", "") for l in f), **kw)
    hdr = next(rd); w = len(hdr); widths = collections.Counter(); shown = 0; n = 0
    ci = hdr.index("cluster_id"); idi = hdr.index("id"); badid = 0
    for row in rd:
        n += 1; widths[len(row)] += 1
        if len(row) == w and not (row[idi].isdigit() and row[ci].isdigit()): badid += 1
        if len(row) != w and shown < 4:
            shown += 1
            print("  BAD row %d width %d: first=%r ... last fields=%r" % (n, len(row), [x[:60] for x in row[:3]], [x[:80] for x in row[-4:]]))
        if n >= N: break
    for p in procs: p.kill()
    print("DIALECT %s: header width %d, %d rows, widths %s, right-width rows with non-numeric ids %d" % (name, w, n, widths.most_common(6), badid), flush=True)
print("HEADER", hdr)
procs, f = stream(); s = f.read(30_000_000)
for p in procs: p.kill()
print("raw sample: backslash-quote %d, doubled-quote-in-text %d, backslash-backslash %d" % (s.count('\\"'), len(re.findall(r'[^,\n]""[^,\n]', s)), s.count('\\\\')))
for m in itertools.islice(re.finditer(r'[^,\n]""[^,\n]', s), 3): print("  ctx doubled:", repr(s[max(0,m.start()-80):m.end()+80]))
for m in itertools.islice(re.finditer(r'\\"', s), 3): print("  ctx escaped:", repr(s[max(0,m.start()-80):m.end()+80]))
