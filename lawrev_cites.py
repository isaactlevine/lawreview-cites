#!/usr/bin/env python3
"""
Law review citations in the Supreme Court and federal courts of appeals.

Downloads CourtListener's public bulk data, keeps SCOTUS and the thirteen
courts of appeals from --since onward, and pulls out every full citation to
the journals listed in JOURNALS.  Standard library only; needs curl and bzip2
(both ship with macOS).  lbzip2, if installed, makes it faster.

    caffeinate -i python3 lawrev_cites.py            # download, then process
    caffeinate -i python3 lawrev_cites.py --stream   # no big files on disk

Outputs (in ~/lawreview-cites/out by default):
    citations.csv     one row per citation instance, with the citing case
    cases.csv         one row per citing case
    denominators.csv  all cases per court/year/status, for citation rates
    summary.txt       headline counts
"""
import argparse, collections, csv, html, io, os, re, shutil, subprocess, sys, time

# ----------------------------------------------------------------- config
COURTS = ["scotus", "ca1", "ca2", "ca3", "ca4", "ca5", "ca6", "ca7", "ca8",
          "ca9", "ca10", "ca11", "cadc", "cafc"]

# name -> regex for the Bluebook abbreviation (whitespace already collapsed)
JOURNALS = {
    "Harvard Law Review":                    r"Harv\.\s?L\.\s?Rev\.?",
    "Yale Law Journal":                      r"Yale\s?L\.\s?J\.?",
    "Stanford Law Review":                   r"Stan\.\s?L\.\s?Rev\.?",
    "Columbia Law Review":                   r"Colum\.\s?L\.\s?Rev\.?",
    "University of Chicago Law Review":      r"U\.\s?Chi\.\s?L\.\s?Rev\.?",
    "NYU Law Review":                        r"N\.\s?Y\.\s?U\.\s?L\.\s?Rev\.?",
    "University of Pennsylvania Law Review": r"U\.\s?Pa\.\s?L\.\s?Rev\.?",
    "Virginia Law Review":                   r"Va\.\s?L\.\s?Rev\.?",
    "Michigan Law Review":                   r"Mich\.\s?L\.\s?Rev\.?",
    "California Law Review":                 r"Cal(?:if)?\.\s?L\.\s?Rev\.?",
}
COMPANION = (r"(?:F\.|Forum|Online|Sidebar|Dialogue|PENNumbra|First Impressions|"
             r"In Brief|Circuit|Pocket Part|Headnotes|Bull\.)")

BASES = ["https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/",
         "https://com-courtlistener-storage.s3.amazonaws.com/"]
FILES = ["dockets", "opinion-clusters", "citations", "opinions"]
TEXT_COLS = ["plain_text", "html_with_citations", "html_columbia", "html_lawbox",
             "xml_harvard", "html_anon_2020", "html"]

csv.field_size_limit(2**31 - 1)

# ------------------------------------------------------------ extraction
CITE_RE = re.compile(
    r"(?<![\w.,])(?P<vol>\d{1,4})\s(?P<j>" +
    "|".join("(?P<j%d>%s)" % (i, p) for i, p in enumerate(JOURNALS.values())) +
    r")(?:\s?(?P<comp>" + COMPANION + r"))?\s(?P<page>\d{1,5})(?![\d.]*\s?(?:U\.\s?S\.|F\.))"
    r"(?P<pins>(?:,\s?(?:at\s)?\d+(?:\s?[-–—]\s?\d+)?(?:\s?(?:&|and)?\s?n+\.\s?\d+)?)*)"
    r"(?:\s?\((?P<paren>[^()]{0,80})\))?")
JNAMES = list(JOURNALS)
PREFILTER = re.compile(r"(?:Harv|Yale|Stan|Colum|Chi|U|Pa|Va|Mich|Cal|Calif)\.?\s*L\.\s*(?:Rev|J)")
TAG_RE = re.compile(r"<[^>]{0,2000}>")
WS_RE = re.compile(r"\s+")
YEAR_RE = re.compile(r"\b(1[89]\d\d|20\d\d)\b")
BREAK_RE = re.compile(r"(?:;|[a-z]{3,}\.|[\)\]\d\"”]\.)\s")
INNER_SIGNAL_RE = re.compile(r"(?:e\.g\.,|\b(?:See|Cf\.|Accord|But see|But cf\.|Compare|Contra)(?: also| generally)?,?)\s")
SIGNAL_RE = re.compile(
    r"^[\s(\[\"“]*(?:(?:see generally|see also|see, e\.g\.,|see|but see|but cf\.|cf\.|accord|compare|"
    r"contra|e\.g\.,|and|with|citing|quoting|discussing|generally|also|in|,)\s+)*", re.I)
SUFFIX_RE = re.compile(r"^(Jr\.|Sr\.|II|III|IV|et al\.),\s?")
STUDENT_RE = re.compile(r"^(Note|Comment|Recent Case|Recent Cases|Case Comment|Case Note|Recent Development|"
                        r"Developments in the Law|The Supreme Court)\b", re.I)
SECTION_RE = re.compile(
    r"(?:JUSTICE|Justice|JUDGE|Judge|J\.)\s[^.;:()]{0,120}?\b(concurring|dissenting)\b|"
    r"\b([A-Z][A-Za-z'’\-]+),\s(?:Circuit|Chief|Senior Circuit)?\s?(?:Judge|Justice|J\.),[^.;()]{0,80}?\b(concurring|dissenting)\b")


def clean_text(raw):
    if "<" in raw and ">" in raw:
        raw = TAG_RE.sub(" ", raw)
    if "&" in raw:
        raw = html.unescape(raw)
    return WS_RE.sub(" ", raw.replace("\xa0", " "))


def parse_prefix(prefix):
    """Best-effort author and title from the text just before '123 Harv. L. Rev.'"""
    cut = 0
    for m in BREAK_RE.finditer(prefix):
        cut = m.end()
    seg = prefix[cut:]
    cut = 0
    for m in INNER_SIGNAL_RE.finditer(seg):
        cut = m.end()
    seg = seg[cut:]
    seg = seg[SIGNAL_RE.match(seg).end():].strip().rstrip(",").strip()
    if not seg or len(seg) > 330:
        return "", "", ""
    kind = "article"
    if STUDENT_RE.match(seg):
        kind = "student/unsigned"
        head, _, rest = seg.partition(", ")
        return ("", rest or head, kind)
    author, sep, title = seg.partition(", ")
    while sep:
        m = SUFFIX_RE.match(title)
        if not m:
            break
        author += " " + m.group(1)
        title = title[m.end():]
    if STUDENT_RE.match(title):
        kind = "student/unsigned"
        title = title.partition(", ")[2] or title
    if not sep or len(author) > 90 or not author[:1].isupper() or re.search(r"\d", author):
        return "", seg, kind
    return author.strip(), title.strip(), kind


def extract(text):
    """Yield one dict per full citation in a cleaned opinion text."""
    sections = [(m.start(), (m.group(1) or m.group(3))) for m in SECTION_RE.finditer(text)]
    for m in CITE_RE.finditer(text):
        ji = next(i for i in range(len(JNAMES)) if m.group("j%d" % i))
        paren = m.group("paren") or ""
        years = YEAR_RE.findall(paren)
        author, title, kind = parse_prefix(text[max(0, m.start() - 400):m.start()])
        sec = ""
        for pos, lab in sections:
            if pos < m.start():
                sec = lab
            else:
                break
        yield {
            "journal": JNAMES[ji], "companion": m.group("comp") or "",
            "volume": m.group("vol"), "first_page": m.group("page"),
            "pinpoints": (m.group("pins") or "").strip(", "), "article_year": years[-1] if years else "",
            "author_raw": author, "title_raw": title, "kind": kind, "section_guess": sec,
            "char_pos": m.start(), "text_len": len(text),
            "context": text[max(0, m.start() - 250):m.end() + 120],
        }


# ------------------------------------------------------------------- I/O
def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def curl_text(url):
    r = subprocess.run(["curl", "-sSL", "--retry", "5", url], capture_output=True)
    if r.returncode:
        raise RuntimeError(r.stderr.decode(errors="replace"))
    return r.stdout.decode("utf-8", errors="replace")


def discover(date=None):
    """Find the newest snapshot of each bulk file by listing the public bucket."""
    last_err = None
    for base in BASES:
        try:
            keys, token = {}, None
            while True:
                url = base + "?list-type=2&prefix=bulk-data/" + ("&continuation-token=" + token if token else "")
                xml = curl_text(url)
                for k, size in re.findall(r"<Key>([^<]+)</Key>.*?<Size>(\d+)</Size>", xml, re.S):
                    keys[k] = int(size)
                t = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
                if not t:
                    break
                token = t.group(1).replace("+", "%2B").replace("/", "%2F").replace("=", "%3D")
            found = {}
            for name in FILES:
                pat = re.compile(r"^bulk-data/%s-(\d{4}-\d{2}-\d{2})\.csv\.bz2$" % re.escape(name))
                cands = sorted((pat.match(k).group(1), k) for k in keys if pat.match(k))
                if date:
                    cands = [c for c in cands if c[0] == date]
                if cands:
                    found[name] = (base + cands[-1][1], keys[cands[-1][1]])
            if all(n in found for n in FILES):
                return found
            last_err = "missing %s among %d keys, e.g. %s" % (
                [n for n in FILES if n not in found], len(keys), sorted(keys)[-15:])
        except Exception as e:  # try the next base
            last_err = repr(e)
    sys.exit("Could not find the bulk files: %s\nSend this message back to Claude." % last_err)


def download(url, size, dest):
    if os.path.exists(dest) and os.path.getsize(dest) == size:
        log("already downloaded", os.path.basename(dest))
        return
    log("downloading %s (%.1f GB)" % (os.path.basename(dest), size / 1e9))
    for attempt in range(30):
        subprocess.run(["curl", "-L", "-C", "-", "--retry", "20", "--retry-delay", "5",
                        "--progress-bar", "-o", dest, url])
        if os.path.exists(dest) and os.path.getsize(dest) == size:
            return
        time.sleep(10)
    sys.exit("Download kept failing for %s; run the same command again to resume." % url)


def decompressor():
    for c in ("lbzip2", "pbzip2", "bzip2"):
        if shutil.which(c):
            return [c, "-dc"]
    sys.exit("bzip2 not found")


class Rows:
    """Iterate dict-free rows of a Postgres-style CSV (bz2 file, URL or plain csv)."""

    def __init__(self, src):
        self.src, self.procs = src, []
        if src.startswith("http"):
            p1 = subprocess.Popen(["curl", "-sSL", "--retry", "20", src], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(decompressor(), stdin=p1.stdout, stdout=subprocess.PIPE)
            p1.stdout.close()
            self.procs, raw = [p1, p2], p2.stdout
        elif src.endswith(".bz2"):
            p = subprocess.Popen(decompressor() + [src], stdout=subprocess.PIPE)
            self.procs, raw = [p], p.stdout
        else:
            raw = open(src, "rb")
        self.f = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        head = []
        for line in self.f:
            head.append(line)
            if len(head) >= 400:
                break
        sample = "".join(head)
        quote = "`" if sample.count("`,`") > sample.count('","') else '"'
        doubled = len(re.findall(r'[^,\n]%s%s[^,\n]' % (quote, quote), sample))
        if doubled > sample.count("\\" + quote):
            kw = dict(quotechar=quote, doublequote=True)
        else:
            kw = dict(quotechar=quote, doublequote=False, escapechar="\\")
        import itertools
        lines = (l.replace("\x00", "") if "\x00" in l else l for l in itertools.chain(head, self.f))
        self.dialect = kw
        self.reader = csv.reader(lines, **kw)
        self.header = [h.strip().strip("`\"") for h in next(self.reader)]
        self.bad = self.n = 0

    def col(self, name, required=True):
        if name in self.header:
            return self.header.index(name)
        if required:
            sys.exit("Column %r not in %s: %s\nSend this message back to Claude." % (name, self.src, self.header))
        return None

    def __iter__(self):
        width = len(self.header)
        for row in self.reader:
            self.n += 1
            if len(row) != width:
                self.bad += 1
                continue
            yield row
        for p in self.procs:
            if p.wait() != 0:
                sys.exit("Reading %s failed partway (exit %s). Re-run to resume the download." % (self.src, p.returncode))
        if self.bad > max(50, self.n * 0.01):
            sys.exit("%d of %d rows in %s did not parse; the file format has changed.\n"
                     "Send this message back to Claude." % (self.bad, self.n, self.src))
        if self.bad:
            log("note: skipped %d malformed rows of %d in %s" % (self.bad, self.n, os.path.basename(self.src)))


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.expanduser("~/lawreview-cites"))
    ap.add_argument("--since", default="2016-01-01")
    ap.add_argument("--stream", action="store_true", help="process straight from the network, saving nothing big")
    ap.add_argument("--date", help="use a specific snapshot date, e.g. 2026-09-30")
    ap.add_argument("--probe", action="store_true", help="read the first rows of each file, report the format, and exit")
    ap.add_argument("--local", help="folder that already holds the four files (testing)")
    a = ap.parse_args()
    data, out = os.path.join(a.dir, "data"), os.path.join(a.dir, "out")
    os.makedirs(data, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    t0 = time.time()

    if a.local:
        src = {}
        for n in FILES:
            c = sorted(f for f in os.listdir(a.local) if re.match(re.escape(n) + r"(-[\d-]+)?\.csv(\.bz2)?$", f))
            src[n] = os.path.join(a.local, c[-1])
        snapshot = "local"
    else:
        found = discover(a.date)
        snapshot = re.search(r"(\d{4}-\d{2}-\d{2})", found["opinions"][0]).group(1)
        total = sum(s for _, s in found.values())
        log("snapshot %s, %.1f GB compressed in total" % (snapshot, total / 1e9))
        if a.stream:
            src = {n: u for n, (u, _) in found.items()}
        else:
            have = sum(os.path.getsize(os.path.join(data, f)) for f in os.listdir(data))
            free = shutil.disk_usage(data).free
            if free < (total - have) * 1.05:
                sys.exit("Need about %.0f GB free but only %.0f GB is available. Free up space, or add "
                         "--stream to process without saving the files." % ((total - have) / 1e9, free / 1e9))
            src = {}
            for n, (u, s) in found.items():
                src[n] = os.path.join(data, os.path.basename(u))
                download(u, s, src[n])

    if a.probe:
        for n in FILES:
            r = Rows(src[n])
            width, bad, rows = len(r.header), 0, 0
            for row in r.reader:
                rows += 1
                bad += len(row) != width
                if rows >= 300:
                    break
            for p in r.procs:
                p.kill()
            print("PROBE %s: %d columns, %d/%d rows malformed, dialect %s\n  %s" % (n, width, bad, rows, r.dialect, r.header), flush=True)
            if bad > 3:
                sys.exit("PROBE FAILED for %s" % n)
        return

    # 1. dockets -> court
    log("pass 1/4: dockets")
    r = Rows(src["dockets"]); ci, ct = r.col("id"), r.col("court_id")
    courts = set(COURTS)
    docket_court = {}
    for row in r:
        if row[ct] in courts:
            docket_court[int(row[ci])] = row[ct]
        if r.n % 5_000_000 == 0:
            log("  %dM dockets read, %d kept" % (r.n // 1_000_000, len(docket_court)))
    log("  %d dockets in target courts" % len(docket_court))

    # 2. clusters
    log("pass 2/4: opinion clusters")
    r = Rows(src["opinion-clusters"])
    c = {k: r.col(k, k in ("id", "docket_id", "date_filed")) for k in
         ("id", "docket_id", "date_filed", "case_name", "case_name_short", "case_name_full", "slug",
          "precedential_status", "judges", "scdb_id", "citation_count")}
    g = lambda row, k: row[c[k]] if c[k] is not None else ""
    clusters, denom = {}, collections.Counter()
    for row in r:
        d = row[c["docket_id"]]
        court = docket_court.get(int(d)) if d.isdigit() else None
        if court and row[c["date_filed"]] >= a.since:
            name = g(row, "case_name") or g(row, "case_name_short") or g(row, "case_name_full")[:200]
            clusters[int(row[c["id"]])] = [court, row[c["date_filed"]], name, g(row, "slug"),
                                           g(row, "precedential_status"), g(row, "judges"), g(row, "scdb_id"), ""]
            denom[(court, row[c["date_filed"]][:4], g(row, "precedential_status"))] += 1
    del docket_court
    log("  %d cases since %s in SCOTUS and the courts of appeals" % (len(clusters), a.since))

    # 3. reporter citations
    log("pass 3/4: reporter citations")
    r = Rows(src["citations"])
    cc, cv, cr, cp = r.col("cluster_id"), r.col("volume"), r.col("reporter"), r.col("page")
    for row in r:
        k = int(row[cc]) if row[cc].isdigit() else None
        if k in clusters:
            cite = "%s %s %s" % (row[cv], row[cr], row[cp])
            cur = clusters[k][7]
            best = ("U.S." in cite and "U.S." not in cur) or not cur
            clusters[k][7] = (cite + ("; " + cur if cur else "")) if best else cur + "; " + cite

    # 4. opinions
    log("pass 4/4: opinion texts (the long one)")
    r = Rows(src["opinions"])
    oi, oc = r.col("id"), r.col("cluster_id")
    oty, oau, opc = r.col("type", False), r.col("author_str", False), r.col("per_curiam", False)
    tcols = [r.col(t, False) for t in TEXT_COLS]
    tcols = [t for t in tcols if t is not None]
    if not tcols:
        sys.exit("No text columns in opinions file: %s\nSend this message back to Claude." % r.header)
    fields = ["cluster_id", "opinion_id", "court", "date_filed", "case_name", "reporter_cite",
              "precedential_status", "opinion_type", "opinion_author", "per_curiam", "section_guess",
              "journal", "companion", "volume", "first_page", "pinpoints", "article_year",
              "author_raw", "title_raw", "kind", "char_pos", "text_len", "url", "context"]
    fc = open(os.path.join(out, "citations.csv"), "w", newline="", encoding="utf-8")
    w = csv.DictWriter(fc, fieldnames=fields)
    w.writeheader()
    citing, scanned, ncite = set(), collections.Counter(), 0
    tally = collections.defaultdict(set)
    for row in r:
        if r.n % 500_000 == 0:
            log("  %.1fM opinions read, %d citations in %d cases, %.0f min elapsed"
                % (r.n / 1e6, ncite, len(citing), (time.time() - t0) / 60))
        k = int(row[oc]) if row[oc].isdigit() else None
        cl = clusters.get(k)
        if cl is None:
            continue
        raw = next((row[t] for t in tcols if row[t]), "")
        if not raw:
            continue
        scanned[(cl[0], cl[1][:4], cl[4])] += 1
        if not PREFILTER.search(raw):
            continue
        url = "https://www.courtlistener.com/opinion/%d/%s/" % (k, cl[3] or "x")
        for hit in extract(clean_text(raw)):
            hit.update(cluster_id=k, opinion_id=row[oi], court=cl[0], date_filed=cl[1], case_name=cl[2],
                       reporter_cite=cl[7], precedential_status=cl[4],
                       opinion_type=row[oty] if oty is not None else "",
                       opinion_author=row[oau] if oau is not None else "",
                       per_curiam=row[opc] if opc is not None else "", url=url)
            w.writerow(hit)
            ncite += 1
            citing.add(k)
            art = (hit["journal"], hit["companion"], hit["volume"], hit["first_page"])
            tally[(hit["journal"], "SCOTUS" if cl[0] == "scotus" else "Courts of appeals")].add((k, art))
    fc.close()

    with open(os.path.join(out, "cases.csv"), "w", newline="", encoding="utf-8") as f:
        cw = csv.writer(f)
        cw.writerow(["cluster_id", "court", "date_filed", "case_name", "slug", "precedential_status",
                     "judges", "scdb_id", "reporter_cite", "url"])
        for k in sorted(citing):
            cl = clusters[k]
            cw.writerow([k] + cl + ["https://www.courtlistener.com/opinion/%d/%s/" % (k, cl[3] or "x")])
    with open(os.path.join(out, "denominators.csv"), "w", newline="", encoding="utf-8") as f:
        dw = csv.writer(f)
        dw.writerow(["court", "year", "precedential_status", "cases", "opinions_with_text"])
        for key in sorted(set(denom) | set(scanned)):
            dw.writerow(list(key) + [denom[key], scanned[key]])

    lines = ["Snapshot: %s   Since: %s   Run time: %.0f min" % (snapshot, a.since, (time.time() - t0) / 60),
             "Cases in scope: %d   Cases citing a tracked journal: %d   Citation instances: %d"
             % (len(clusters), len(citing), ncite), "",
             "Distinct article-case pairs", "%-40s %8s %18s" % ("Journal", "SCOTUS", "Courts of appeals")]
    for j in sorted(JNAMES, key=lambda j: -len(tally[(j, "SCOTUS")]) - len(tally[(j, "Courts of appeals")])):
        lines.append("%-40s %8d %18d" % (j, len(tally[(j, "SCOTUS")]), len(tally[(j, "Courts of appeals")])))
    open(os.path.join(out, "summary.txt"), "w").write("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))
    log("done. Results are in", out)


if __name__ == "__main__":
    main()
