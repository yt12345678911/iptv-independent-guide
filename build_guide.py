#!/usr/bin/env python3
"""Independent XMLTV guide builder (standard library only).

Downloads exactly two public XMLTV feeds (UK1, DK1), keeps only the channels in
mapping.json, validates the result, and atomically writes a slim guide.

Safety properties:
  * Only the two hard-coded HTTPS URLs may be contacted; anything else, and any
    redirect, is refused before a connection is made.
  * Compressed and decompressed sizes are bounded; DOCTYPE/ENTITY declarations
    are rejected before parsing (no external DTDs or entities).
  * The output (and report) is written only after every validation passes, via
    a temp file + os.replace. On any failure the previous files are untouched
    and the exit code is non-zero.
  * Production mode refuses any mapping whose status is not "verified".
    --local-preview allows provisional mappings for local testing only, marks
    the output NOT FOR RELEASE, and refuses to run under CI.
    --test-preview allows provisional mappings for the scheduled cloud TEST
    preview. Under CI it runs only on the exact TEST_REPOSITORY / TEST_REF;
    locally it needs the explicit flag. Its output carries an XML comment
    naming every unverified mapping, a TEST generator name and status mode
    "test-preview". It is never production.

Exit codes: 0 ok, 1 build/validation failure, 2 usage/config error.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ALLOWED_SOURCES = {
    "UK1": "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz",
    "DK1": "https://epgshare01.online/epgshare01/epg_ripper_DK1.xml.gz",
}
ALLOWED_HOST = "epgshare01.online"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

MAX_COMPRESSED = 20 * 1024 * 1024
MAX_DECOMPRESSED = 120 * 1024 * 1024
FETCH_TIMEOUT = 60

WINDOW_PAST = timedelta(hours=2)
WINDOW_FUTURE = timedelta(hours=48)
REQUIRED_HORIZON = timedelta(hours=24)
MAX_GAP = timedelta(minutes=60)
MAX_OVERLAP = timedelta(minutes=5)
MAX_DURATION = timedelta(hours=12)

PREVIEW_MARK = "LOCAL PREVIEW - NOT FOR RELEASE"

TEST_MODE = "test-preview"
TEST_GENERATOR = "independent-guide TEST PREVIEW - unverified mapping, not production"
TEST_REPOSITORY = "yt12345678911/iptv-independent-guide"
TEST_REF = "refs/heads/test"


class BuildError(Exception):
    """Any condition that must stop publication."""


# --------------------------------------------------------------------------- fetch

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BuildError(f"redirect refused ({code})")


def check_url(url: str) -> None:
    """Refuse anything but the exact allowed feed URLs. Called before any contact."""
    if url not in ALLOWED_SOURCES.values():
        raise BuildError("URL not in allowlist")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != ALLOWED_HOST or parts.port not in (None, 443):
        raise BuildError("URL scheme/host not allowed")


def fetch_source(url: str) -> bytes:
    check_url(url)
    opener = urllib.request.build_opener(_NoRedirect())
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
            if resp.status != 200:
                raise BuildError(f"HTTP {resp.status}")
            if resp.geturl() != url:
                raise BuildError("final URL changed")
            data = resp.read(MAX_COMPRESSED + 1)
    except urllib.error.URLError as exc:
        raise BuildError(f"fetch failed: {exc.reason if hasattr(exc, 'reason') else exc}") from None
    if len(data) > MAX_COMPRESSED:
        raise BuildError("compressed download exceeds size limit")
    return data


def decompress(data: bytes) -> bytes:
    """gzip -> bytes with a decompressed-size bound. Plain XML passes through (bounded)."""
    if data[:2] != b"\x1f\x8b":
        if len(data) > MAX_DECOMPRESSED:
            raise BuildError("XML exceeds size limit")
        return data
    out = io.BytesIO()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            while True:
                chunk = gz.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                if out.tell() > MAX_DECOMPRESSED:
                    raise BuildError("decompressed feed exceeds size limit")
    except (OSError, EOFError) as exc:
        raise BuildError(f"bad gzip: {exc}") from None
    return out.getvalue()


# --------------------------------------------------------------------------- parse

def parse_time(value: str | None) -> datetime:
    """XMLTV 'YYYYmmddHHMMSS +ZZZZ'. An explicit offset is mandatory."""
    if not value:
        raise BuildError("missing timestamp")
    value = value.strip()
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S %z").astimezone(timezone.utc)
    except ValueError:
        raise BuildError(f"timestamp without valid timezone: {value!r}") from None


def fmt_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def _reject_dtd(xml: bytes) -> None:
    # Declarations may only appear before the root element; scan the prolog,
    # and also refuse ENTITY anywhere as belt-and-braces.
    root_at = xml.find(b"<tv")
    prolog = xml[: root_at if root_at >= 0 else 4096]
    if b"<!DOCTYPE" in prolog.upper() or b"<!ENTITY" in xml:
        raise BuildError("DOCTYPE/ENTITY declarations are not allowed")


def parse_feed(xml: bytes, wanted: set[str]) -> dict[str, list[dict]]:
    """Return {source_id: [programme dict, ...]} for wanted ids only."""
    _reject_dtd(xml)
    found: dict[str, list[dict]] = {sid: [] for sid in wanted}
    declared: set[str] = set()
    try:
        for _event, elem in ET.iterparse(io.BytesIO(xml), events=("end",)):
            if elem.tag == "channel":
                if elem.get("id") in wanted:
                    declared.add(elem.get("id"))
                elem.clear()
            elif elem.tag == "programme":
                sid = elem.get("channel")
                if sid in wanted:
                    titles = [t for t in elem.findall("title") if (t.text or "").strip()]
                    prog = {
                        "start": parse_time(elem.get("start")),
                        "stop": parse_time(elem.get("stop")),
                        "title": titles[0].text.strip() if titles else "",
                        "title_lang": titles[0].get("lang") if titles else None,
                    }
                    for tag in ("sub-title", "desc"):
                        child = elem.find(tag)
                        if child is not None and (child.text or "").strip():
                            prog[tag] = (child.text.strip(), child.get("lang"))
                    cats = [c.text.strip() for c in elem.findall("category") if (c.text or "").strip()]
                    if cats:
                        prog["category"] = cats[:3]
                    found[sid].append(prog)
                elem.clear()
    except ET.ParseError as exc:
        raise BuildError(f"XML parse error: {exc}") from None
    missing = sorted(wanted - declared)
    if missing:
        raise BuildError(f"channel(s) not declared in feed: {', '.join(missing)}")
    return found


# --------------------------------------------------------------------------- mapping

def load_mapping(path: Path, allow_provisional: bool) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"cannot read mapping: {exc}") from None
    if not isinstance(data, dict):
        raise BuildError("mapping: root must be a JSON object")
    channels = data.get("channels")
    if data.get("schema") != 1 or not isinstance(channels, list) or not channels:
        raise BuildError("mapping: unsupported schema or empty channel list")
    seen_ids, seen_src = set(), set()
    for i, ch in enumerate(channels):
        if not isinstance(ch, dict):
            raise BuildError(f"mapping[{i}]: entry must be a JSON object")
        for key in ("id", "display_name", "source", "source_id", "status"):
            if not isinstance(ch.get(key), str) or not ch[key].strip():
                raise BuildError(f"mapping[{i}]: missing {key}")
        if ch["source"] not in ALLOWED_SOURCES:
            raise BuildError(f"mapping[{i}]: unknown source {ch['source']!r}")
        if ch["status"] not in ("verified", "provisional"):
            raise BuildError(f"mapping[{i}]: invalid status {ch['status']!r}")
        if ch["id"] in seen_ids:
            raise BuildError(f"mapping: duplicate id {ch['id']}")
        if (ch["source"], ch["source_id"]) in seen_src:
            raise BuildError(f"mapping: duplicate source id {ch['source_id']}")
        seen_ids.add(ch["id"])
        seen_src.add((ch["source"], ch["source_id"]))
    unverified = [c["id"] for c in channels if c["status"] != "verified"]
    if unverified and not allow_provisional:
        raise BuildError("unverified mapping(s) block production publication: " + ", ".join(unverified))
    return channels


# --------------------------------------------------------------------------- validate

def select_and_validate(channels, programmes_by_source, now: datetime):
    """Window-filter, then validate each channel. Returns ({id: [prog]}, stats)."""
    lo, hi = now - WINDOW_PAST, now + WINDOW_FUTURE
    out, stats, problems = {}, {}, []
    for ch in channels:
        cid = ch["id"]
        progs = sorted(programmes_by_source[ch["source"]][ch["source_id"]], key=lambda p: p["start"])
        for p in progs:
            if p["stop"] <= p["start"]:
                problems.append(f"{cid}: non-positive duration at {fmt_time(p['start'])}")
            elif p["stop"] - p["start"] > MAX_DURATION:
                problems.append(f"{cid}: implausible duration at {fmt_time(p['start'])}")
        kept = [p for p in progs if p["stop"] > lo and p["start"] < hi]
        starts = [p["start"] for p in kept]
        if len(starts) != len(set(starts)):
            problems.append(f"{cid}: duplicate programme start times")
        if any(not p["title"] for p in kept):
            problems.append(f"{cid}: programme(s) with empty title")
        for a, b in zip(kept, kept[1:]):
            if b["start"] - a["stop"] > MAX_GAP:
                problems.append(f"{cid}: gap {b['start'] - a['stop']} at {fmt_time(a['stop'])}")
            if a["stop"] - b["start"] > MAX_OVERLAP:
                problems.append(f"{cid}: overlap {a['stop'] - b['start']} at {fmt_time(b['start'])}")
        current = [p for p in kept if p["start"] <= now < p["stop"]]
        nxt = [p for p in kept if p["start"] >= now]
        horizon = max((p["stop"] for p in kept), default=None)
        if not current:
            problems.append(f"{cid}: no programme airing now (stale or missing data)")
        if not nxt:
            problems.append(f"{cid}: no next programme")
        if horizon is None or horizon < now + REQUIRED_HORIZON:
            problems.append(f"{cid}: schedule ends before now+24h")
        out[cid] = kept
        stats[cid] = {
            "source": ch["source"],
            "source_id": ch["source_id"],
            "status": ch["status"],
            "programmes": len(kept),
            "now": current[0]["title"] if current else None,
            "next": nxt[0]["title"] if nxt else None,
            "first_start": fmt_time(kept[0]["start"]) if kept else None,
            "last_stop": fmt_time(horizon) if horizon else None,
            "hours_ahead": round((horizon - now).total_seconds() / 3600, 1) if horizon else None,
        }
    if problems:
        raise BuildError("validation failed:\n  " + "\n  ".join(problems))
    return out, stats


# --------------------------------------------------------------------------- write

def test_comment(channels) -> str:
    """The marker every --test-preview guide must carry (valid XML comment text)."""
    ids = [c["id"] for c in channels if c["status"] != "verified"]
    text = "TEST - unverified mapping: " + (", ".join(ids) if ids else "none")
    if "--" in text or text.endswith("-"):
        raise BuildError("channel id cannot be placed in an XML comment")
    return text


def render(channels, selected, preview: bool, test: bool = False) -> bytes:
    generator = TEST_GENERATOR if test else PREVIEW_MARK if preview else "independent-guide"
    tv = ET.Element("tv", {"generator-info-name": generator})
    if test:
        tv.append(ET.Comment(f" {test_comment(channels)} "))
    for ch in channels:
        c = ET.SubElement(tv, "channel", {"id": ch["id"]})
        ET.SubElement(c, "display-name").text = ch["display_name"]
    for ch in channels:
        for p in selected[ch["id"]]:
            pe = ET.SubElement(tv, "programme", {
                "start": fmt_time(p["start"]), "stop": fmt_time(p["stop"]), "channel": ch["id"]})
            t = ET.SubElement(pe, "title", {"lang": p["title_lang"]} if p["title_lang"] else {})
            t.text = p["title"]
            for tag in ("sub-title", "desc"):
                if tag in p:
                    text, lang = p[tag]
                    ET.SubElement(pe, tag, {"lang": lang} if lang else {}).text = text
            for cat in p.get("category", []):
                ET.SubElement(pe, "category").text = cat
    ET.indent(tv, space=" ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tv, encoding="utf-8") + b"\n"


def verify_rendered(xml: bytes, channels, test: bool = False) -> None:
    """Re-parse what we are about to publish and check the output contract."""
    root = ET.fromstring(xml)
    if test and (f"<!-- {test_comment(channels)} -->".encode() not in xml
                 or root.get("generator-info-name") != TEST_GENERATOR):
        raise BuildError("rendered test guide lacks its TEST marker")
    ids = [c.get("id") for c in root.findall("channel")]
    if ids != [c["id"] for c in channels] or len(set(ids)) != len(ids):
        raise BuildError("rendered guide channel ids do not match mapping")
    for p in root.findall("programme"):
        if p.get("channel") not in ids or not (p.findtext("title") or "").strip():
            raise BuildError("rendered guide has orphan or untitled programme")


def guard_output(path: Path) -> None:
    # Never write a playlist, into a serve/ tree, or next to a playlist builder
    # (channels.json / build.py mark the project root holding the live outputs).
    resolved = path.resolve()
    if (resolved.suffix.lower() in (".m3u", ".m3u8")
            or "serve" in [p.lower() for p in resolved.parts[:-1]]
            or any((resolved.parent / n).exists() for n in ("channels.json", "build.py"))):
        raise BuildError("refusing to write over the existing playlist/guide outputs or serve/")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- main

def in_ci() -> bool:
    return bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))


def check_test_preview_context() -> None:
    """Under CI, --test-preview runs only on the exact test branch of the exact repo."""
    if not in_ci():
        return  # local: the explicit --test-preview flag is the opt-in
    repo, ref = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_REF", "")
    if repo != TEST_REPOSITORY or ref != TEST_REF:
        raise BuildError(f"test preview refused under CI for {repo or '?'}@{ref or '?'}; "
                         f"only {TEST_REPOSITORY}@{TEST_REF} is allowed")


def build(mapping_path: Path, output: Path, report: Path | None, feeds: dict[str, Path],
          preview: bool, now: datetime, test: bool = False) -> dict:
    guard_output(output)
    if report is not None:
        guard_output(report)
    if preview and test:
        raise BuildError("--local-preview and --test-preview are mutually exclusive")
    if preview and in_ci():
        raise BuildError("local preview mode is not allowed under CI")
    if test:
        check_test_preview_context()
    channels = load_mapping(mapping_path, allow_provisional=preview or test)
    sources_needed = sorted({c["source"] for c in channels})
    programmes_by_source, source_info = {}, {}
    for src in sources_needed:
        if src in feeds:
            try:
                raw = feeds[src].read_bytes()
            except OSError as exc:
                raise BuildError(f"{src}: cannot read offline feed: {exc}") from None
            origin = "offline-file"
        else:
            raw = fetch_source(ALLOWED_SOURCES[src])
            origin = "download"
        xml = decompress(raw)
        wanted = {c["source_id"] for c in channels if c["source"] == src}
        programmes_by_source[src] = parse_feed(xml, wanted)
        source_info[src] = {"origin": origin, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(xml)}
    selected, stats = select_and_validate(channels, programmes_by_source, now)
    xml_out = render(channels, selected, preview, test)
    verify_rendered(xml_out, channels, test)
    result = {
        "status": "ok",
        "mode": TEST_MODE if test else PREVIEW_MARK if preview else "production",
        "production": not (preview or test),
        "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "output_sha256": hashlib.sha256(xml_out).hexdigest(),
        "sources": source_info,
        "channels": stats,
    }
    if preview:
        result["warning"] = ("NOT FOR RELEASE: provisional mappings included: "
                             + ", ".join(c["id"] for c in channels if c["status"] != "verified"))
    if test:
        result["warning"] = test_comment(channels) + " (preview only, not production)"
    atomic_write(output, xml_out)
    if report is not None:
        atomic_write(report, (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mapping", type=Path, default=Path(__file__).with_name("mapping.json"))
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--report", type=Path, help="write a JSON build report (only on success)")
    ap.add_argument("--feed", action="append", default=[], metavar="SRC=PATH",
                    help="use a local .xml or .xml.gz file instead of downloading (testing)")
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--local-preview", action="store_true",
                       help=f"allow provisional mappings; output is marked {PREVIEW_MARK}")
    modes.add_argument("--test-preview", action="store_true",
                       help=f"scheduled cloud TEST preview; under CI only {TEST_REPOSITORY}@{TEST_REF}")
    ap.add_argument("--now", help="override current time (ISO 8601 with offset), for tests")
    args = ap.parse_args(argv)

    feeds = {}
    for item in args.feed:
        src, sep, path = item.partition("=")
        if not sep or src not in ALLOWED_SOURCES:
            print(f"error: --feed must be one of {sorted(ALLOWED_SOURCES)}=PATH", file=sys.stderr)
            return 2
        feeds[src] = Path(path)
    if args.now:
        now = datetime.fromisoformat(args.now)
        if now.tzinfo is None:
            print("error: --now needs a timezone offset", file=sys.stderr)
            return 2
        now = now.astimezone(timezone.utc)
    else:
        now = datetime.now(timezone.utc)
    if args.local_preview:
        print(f"WARNING: {PREVIEW_MARK} (provisional mappings allowed)", file=sys.stderr)
    if args.test_preview:
        print("WARNING: TEST preview (provisional mappings allowed, not production)", file=sys.stderr)
    try:
        result = build(args.mapping, args.output, args.report, feeds, args.local_preview, now,
                       test=args.test_preview)
    except BuildError as exc:
        print(f"BUILD FAILED, previous guide left unchanged: {exc}", file=sys.stderr)
        return 1
    print(f"{result['mode']}: wrote {args.output} ({len(result['channels'])} channels)")
    for cid, s in result["channels"].items():
        print(f"  {cid:14} {s['status']:11} {s['programmes']:4} progs  +{s['hours_ahead']}h  now: {s['now']}")
    if args.local_preview or args.test_preview:
        print(f"WARNING: {result['warning']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
