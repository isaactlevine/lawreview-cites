import csv, io, subprocess, sys, collections
import lawrev_cites as L
csv.field_size_limit(2**31 - 1)
url = L.discover()["opinions"][0]
N = int(sys.argv[1])
p1 = subprocess.Popen(["curl", "-sSL", url], stdout=subprocess.PIPE)
p2 = subprocess.Popen(L.decompressor(), stdin=p1.stdout, stdout=subprocess.PIPE)
f = io.TextIOWrapper(p2.stdout, encoding="utf-8", errors="replace", newline="")
rd = csv.reader((l.replace("\x00", "") for l in f), quotechar='"', doublequote=False, escapechar="\\")
hdr = next(rd); w = len(hdr); n = 0; shown = 0
buckets = collections.Counter(); widths = collections.Counter(); prev = None
for row in rd:
    n += 1
    if len(row) != w:
        buckets[n // 100000] += 1; widths[len(row)] += 1
        if shown < 40:
            shown += 1
            print("BAD n=%d width=%d prev_id=%s lens=%s\n   head=%r\n   tail=%r" % (
                n, len(row), prev, [len(x) for x in row][:30], [x[:50] for x in row[:4]], [x[-120:] for x in row[-3:]]), flush=True)
    else:
        prev = row[0]
    if n >= N: break
p1.kill(); p2.kill()
print("TOTAL rows %d, bad %d, bad widths %s" % (n, sum(buckets.values()), widths.most_common(10)))
print("bad per 100k bucket:", sorted(buckets.items()))
