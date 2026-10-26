"""Unit tests for build_guide.py. Synthetic fixtures only; no network.

Run from independent/guide:  python -m unittest discover -s tests -v
"""
import gzip
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import build_guide as bg  # noqa: E402

NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


def mapping(statuses=("verified", "verified")):
    return {
        "schema": 1,
        "channels": [
            {"id": "one.test", "display_name": "One", "source": "UK1", "source_id": "ONE.uk", "status": statuses[0]},
            {"id": "two.test", "display_name": "Two", "source": "DK1", "source_id": "TWO.dk", "status": statuses[1]},
        ],
    }


def schedule(start=NOW - timedelta(hours=6), hours=60, step=timedelta(minutes=30), title="Show"):
    out, t = [], start
    while t < start + timedelta(hours=hours):
        out.append((t, t + step, f"{title} {t:%H%M}"))
        t += step
    return out


def feed_xml(channels: dict, prolog: str = "", tz=" +0000") -> bytes:
    parts = [f'<?xml version="1.0" encoding="UTF-8"?>{prolog}<tv>']
    for sid in channels:
        parts.append(f'<channel id="{sid}"><display-name>{sid}</display-name><icon src="http://x/i.png"/></channel>')
    parts.append('<channel id="OTHER.uk"><display-name>Other</display-name></channel>')
    for sid, progs in channels.items():
        for start, stop, title in progs:
            s = start.strftime("%Y%m%d%H%M%S") + tz
            e = stop.strftime("%Y%m%d%H%M%S") + tz
            parts.append(f'<programme start="{s}" stop="{e}" channel="{sid}"><title lang="en">{title}</title>'
                         f'<desc>About {title}</desc></programme>')
    for start, stop, title in schedule()[:3]:
        parts.append(f'<programme start="{start:%Y%m%d%H%M%S} +0000" stop="{stop:%Y%m%d%H%M%S} +0000" '
                     f'channel="OTHER.uk"><title>Other</title></programme>')
    parts.append("</tv>")
    return "".join(parts).encode("utf-8")


CI_VARS = ("CI", "GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_REF")


class Base(unittest.TestCase):
    def setUp(self):
        # The suite also runs inside the scheduled workflow: start from a clean, non-CI env.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in CI_VARS:
            os.environ.pop(var, None)
        self.dir = Path(tempfile.mkdtemp(prefix="guide-test-"))
        self.out = self.dir / "public" / "guide.xml"
        self.report = self.dir / "public" / "status.json"
        self.out.parent.mkdir()
        self.out.write_bytes(b"PREVIOUS-GOOD-GUIDE")
        self.write_mapping(mapping())
        self.write_feeds(feed_xml({"ONE.uk": schedule()}), feed_xml({"TWO.dk": schedule()}))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_mapping(self, data):
        self.mapping = self.dir / "mapping.json"
        self.mapping.write_text(json.dumps(data), encoding="utf-8")

    def write_feeds(self, uk: bytes, dk: bytes, gz=True):
        self.uk = self.dir / ("uk.xml.gz" if gz else "uk.xml")
        self.dk = self.dir / "dk.xml.gz"
        self.uk.write_bytes(gzip.compress(uk) if gz else uk)
        self.dk.write_bytes(gzip.compress(dk))

    def run_build(self, preview=False, test=False):
        return bg.build(self.mapping, self.out, self.report, {"UK1": self.uk, "DK1": self.dk}, preview, NOW,
                        test=test)

    def assert_fails_unchanged(self, pattern, preview=False, test=False):
        with self.assertRaisesRegex(bg.BuildError, pattern):
            self.run_build(preview, test)
        self.assertEqual(self.out.read_bytes(), b"PREVIOUS-GOOD-GUIDE")
        self.assertFalse(self.report.exists())
        self.assertEqual([p.name for p in self.out.parent.iterdir()], ["guide.xml"])  # no temp debris


class SuccessTest(Base):
    def test_builds_slim_guide(self):
        result = self.run_build()
        root = ET.fromstring(self.out.read_bytes())
        self.assertEqual([c.get("id") for c in root.findall("channel")], ["one.test", "two.test"])
        progs = root.findall("programme")
        self.assertEqual({p.get("channel") for p in progs}, {"one.test", "two.test"})
        self.assertTrue(all(p.findtext("title") for p in progs))
        self.assertTrue(all(p.get("start").endswith(" +0000") for p in progs))
        self.assertNotIn(b"OTHER", self.out.read_bytes())
        self.assertNotIn(b"icon", self.out.read_bytes())
        # window: nothing ending before now-2h, nothing starting after now+48h
        for p in progs:
            self.assertGreater(bg.parse_time(p.get("stop")), NOW - bg.WINDOW_PAST)
            self.assertLess(bg.parse_time(p.get("start")), NOW + bg.WINDOW_FUTURE)
        self.assertEqual(result["mode"], "production")
        self.assertEqual(json.loads(self.report.read_text())["status"], "ok")

    def test_converts_offsets_to_utc(self):
        dk = feed_xml({"TWO.dk": [(s + timedelta(hours=1), e + timedelta(hours=1), t) for s, e, t in schedule()]},
                      tz=" +0100")
        self.write_feeds(feed_xml({"ONE.uk": schedule()}), dk)
        self.run_build()
        root = ET.fromstring(self.out.read_bytes())
        first = next(p for p in root.findall("programme") if p.get("channel") == "two.test")
        self.assertEqual(first.get("start"), "20300115100000 +0000")

    def test_plain_xml_feed_accepted(self):
        self.write_feeds(feed_xml({"ONE.uk": schedule()}), feed_xml({"TWO.dk": schedule()}), gz=False)
        self.run_build()


class MappingGateTest(Base):
    def test_production_refuses_provisional(self):
        self.write_mapping(mapping(("verified", "provisional")))
        self.assert_fails_unchanged("unverified mapping.*two.test")

    def test_preview_allows_provisional_and_is_marked(self):
        self.write_mapping(mapping(("verified", "provisional")))
        result = self.run_build(preview=True)
        self.assertEqual(result["mode"], bg.PREVIEW_MARK)
        self.assertIn("NOT FOR RELEASE", result["warning"])
        self.assertIn(bg.PREVIEW_MARK.encode(), self.out.read_bytes())

    def test_preview_refused_under_ci(self):
        self.write_mapping(mapping(("verified", "provisional")))
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            self.assert_fails_unchanged("not allowed under CI", preview=True)

    def test_invalid_mappings(self):
        bad = [
            {"schema": 2, "channels": mapping()["channels"]},
            {"schema": 1, "channels": []},
        ]
        m = mapping(); m["channels"][1]["source"] = "XX1"; bad.append(m)
        m = mapping(); m["channels"][1]["status"] = "maybe"; bad.append(m)
        m = mapping(); m["channels"][1]["id"] = "one.test"; bad.append(m)
        m = mapping(); m["channels"][1]["source"] = "UK1"; m["channels"][1]["source_id"] = "ONE.uk"; bad.append(m)
        m = mapping(); del m["channels"][0]["source_id"]; bad.append(m)
        for data in bad:
            with self.subTest(data=data):
                self.write_mapping(data)
                self.assert_fails_unchanged("mapping")

    def test_non_object_mapping_root_and_entries(self):
        for data in ([], [{"schema": 1}], "x", 1, None, {"schema": 1, "channels": ["bbc1.uk"]}):
            with self.subTest(data=data):
                self.write_mapping(data)
                self.assert_fails_unchanged("mapping")
                self.assert_fails_unchanged("mapping", test=True)

    def test_wrong_region_mapping_fails(self):
        m = mapping(); m["channels"][0]["source_id"] = "ONE.Wrong.Region.uk"
        self.write_mapping(m)
        self.assert_fails_unchanged("not declared")


GOOD_CI = {"GITHUB_ACTIONS": "true", "CI": "true",
           "GITHUB_REPOSITORY": "yt12345678911/iptv-independent-guide", "GITHUB_REF": "refs/heads/test"}
REAL_MAPPING = HERE.parent / "mapping.json"
TEST_COMMENT = "TEST - unverified mapping: bbc1.uk, itv1anglia.uk, TV2Lorry.dk"


class TestPreviewGateTest(Base):
    def setUp(self):
        super().setUp()
        self.write_mapping(mapping(("verified", "provisional")))

    def test_local_test_preview_with_explicit_flag(self):
        result = self.run_build(test=True)
        self.assertEqual(result["mode"], "test-preview")
        self.assertIs(result["production"], False)

    def test_ci_exact_repo_and_ref_allowed(self):
        with mock.patch.dict(os.environ, GOOD_CI):
            self.assertEqual(self.run_build(test=True)["mode"], "test-preview")

    def test_ci_wrong_ref_or_repo_refused(self):
        bad = [
            {"GITHUB_REF": "refs/heads/pages"},
            {"GITHUB_REF": "refs/heads/main"},
            {"GITHUB_REF": "refs/heads/master"},
            {"GITHUB_REF": "refs/heads/test2"},
            {"GITHUB_REF": "refs/tags/test"},
            {"GITHUB_REF": "refs/pull/1/merge"},
            {"GITHUB_REF": ""},
            {"GITHUB_REPOSITORY": "someone-else/iptv-independent-guide"},
            {"GITHUB_REPOSITORY": "yt12345678911/iptv-independent-guide-fork"},
            {"GITHUB_REPOSITORY": "YT12345678911/IPTV-INDEPENDENT-GUIDE"},
            {"GITHUB_REPOSITORY": ""},
        ]
        for override in bad:
            with self.subTest(override=override), mock.patch.dict(os.environ, {**GOOD_CI, **override}):
                self.assert_fails_unchanged("test preview refused under CI", test=True)

    def test_ci_flag_alone_triggers_gate(self):
        for env in ({"CI": "true"}, {"GITHUB_ACTIONS": "true"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assert_fails_unchanged("test preview refused under CI", test=True)

    def test_local_preview_still_refused_under_ci_even_on_test_ref(self):
        with mock.patch.dict(os.environ, GOOD_CI):
            self.assert_fails_unchanged("not allowed under CI", preview=True)

    def test_modes_mutually_exclusive(self):
        self.assert_fails_unchanged("mutually exclusive", preview=True, test=True)
        args = ["--output", str(self.out), "--local-preview", "--test-preview"]
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as cm:
            bg.main(args)
        self.assertEqual(cm.exception.code, 2)

    def test_production_still_refuses_provisional_in_test_ci_context(self):
        with mock.patch.dict(os.environ, GOOD_CI):
            self.assert_fails_unchanged("unverified mapping.*two.test")

    def test_test_preview_keeps_validation(self):
        self.write_feeds(feed_xml({"ONE.uk": schedule(hours=20)}), feed_xml({"TWO.dk": schedule()}))
        self.assert_fails_unchanged("ends before now\\+24h", test=True)


class TestPreviewOutputTest(Base):
    def real_mapping_build(self):
        # Real mapping ids/statuses, synthetic schedules under their real source ids.
        real = json.loads(REAL_MAPPING.read_text(encoding="utf-8"))
        self.write_mapping(real)
        by_src = {"UK1": {}, "DK1": {}}
        for ch in real["channels"]:
            by_src[ch["source"]][ch["source_id"]] = schedule(title=ch["id"])
        self.write_feeds(feed_xml(by_src["UK1"]), feed_xml(by_src["DK1"]))
        with mock.patch.dict(os.environ, GOOD_CI):
            return real, self.run_build(test=True)

    def test_marker_comment_generator_and_status(self):
        real, result = self.real_mapping_build()
        xml = self.out.read_bytes()
        self.assertIn(f"<!-- {TEST_COMMENT} -->".encode(), xml)
        root = ET.fromstring(xml, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
        comments = [e.text for e in root if e.tag is ET.Comment]
        self.assertEqual(comments, [f" {TEST_COMMENT} "])
        self.assertEqual(root.get("generator-info-name"), bg.TEST_GENERATOR)
        self.assertIn("TEST", root.get("generator-info-name"))
        self.assertEqual([c.get("id") for c in root.findall("channel")], [c["id"] for c in real["channels"]])
        self.assertTrue(all((p.findtext("title") or "").strip() for p in root.findall("programme")))
        status = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(status["mode"], "test-preview")
        self.assertIs(status["production"], False)
        self.assertTrue(status["warning"].startswith(TEST_COMMENT))
        for cid, s in status["channels"].items():
            self.assertTrue(s["now"] and s["next"] and s["hours_ahead"] >= 24, cid)
        self.assertNotIn(bg.PREVIEW_MARK.encode(), xml)

    def test_provisional_statuses_unchanged(self):
        before = REAL_MAPPING.read_bytes()
        real, _ = self.real_mapping_build()
        status = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(sorted(c for c, s in status["channels"].items() if s["status"] == "provisional"),
                         ["TV2Lorry.dk", "bbc1.uk", "itv1anglia.uk"])
        self.assertEqual(sorted(c["id"] for c in real["channels"] if c["status"] != "verified"),
                         ["TV2Lorry.dk", "bbc1.uk", "itv1anglia.uk"])
        self.assertEqual(REAL_MAPPING.read_bytes(), before)

    def test_real_mapping_still_blocks_production(self):
        self.real_mapping_build()
        good = self.out.read_bytes()
        self.report.unlink()
        with mock.patch.dict(os.environ, GOOD_CI):
            self.assertRaisesRegex(bg.BuildError, "unverified mapping", self.run_build)
        self.assertEqual(self.out.read_bytes(), good)
        self.assertFalse(self.report.exists())

    def test_comment_rejects_unsafe_ids(self):
        with self.assertRaises(bg.BuildError):
            bg.test_comment([{"id": "a--b", "status": "provisional"}])
        self.assertEqual(bg.test_comment([{"id": "a", "status": "verified"}]), "TEST - unverified mapping: none")


def workflow_text():
    """The active workflow in the public repo, else the template in the parent project."""
    base = HERE.parent
    for path in (base / ".github" / "workflows" / "publish-test.yml",
                 base / "templates" / "publish-test.yml.template"):
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise unittest.SkipTest("publish-test workflow not present")


class WorkflowIsolationTest(unittest.TestCase):
    def setUp(self):
        self.wf = workflow_text()

    def test_schedule_and_triggers(self):
        self.assertRegex(self.wf, r'cron: "23 \*/6 \* \* \*"')
        self.assertIn("workflow_dispatch:", self.wf)
        for trigger in ("push:", "pull_request", "workflow_run", "repository_dispatch"):
            self.assertNotIn(trigger, self.wf)

    def test_exact_repo_and_branch_guards(self):
        self.assertIn("github.repository == 'yt12345678911/iptv-independent-guide' && "
                      "github.ref == 'refs/heads/test'", self.wf)
        self.assertIn('test "$GITHUB_REPOSITORY" = "yt12345678911/iptv-independent-guide"', self.wf)
        self.assertIn('test "$GITHUB_REF" = "refs/heads/test"', self.wf)
        self.assertRegex(self.wf, r"(?m)^\s+ref: test$")
        self.assertRegex(self.wf, r"(?m)^\s+persist-credentials: true$")

    def test_runner_permissions_and_limits(self):
        self.assertIn("runs-on: ubuntu-latest", self.wf)
        self.assertEqual(self.wf.count("runs-on:"), 1)
        self.assertRegex(self.wf, r"(?m)^permissions:\n  contents: read$")
        self.assertRegex(self.wf, r"(?m)^    permissions:\n      contents: write$")
        self.assertIn("timeout-minutes: 10", self.wf)
        self.assertIn("group: publish-test-guide", self.wf)
        self.assertIn("cancel-in-progress: false", self.wf)

    def test_only_pinned_checkout_and_no_secrets_or_storage(self):
        uses = re.findall(r"uses:\s*(\S+)", self.wf)
        self.assertEqual(uses, ["actions/checkout@11d5960a326750d5838078e36cf38b85af677262"])
        self.assertNotRegex(self.wf, r"(?i)secrets\.|upload-artifact|actions/cache|cache:|cloudflare_|api[_-]?token|wrangler")

    def test_builds_test_preview_only(self):
        self.assertIn("build_guide.py --test-preview", self.wf)
        self.assertNotIn("--local-preview", self.wf)
        self.assertIn(TEST_COMMENT, self.wf)
        self.assertIn('"mode": "test-preview"', self.wf)

    def test_writes_only_site_files_to_test(self):
        pushes = re.findall(r"git push.*", self.wf)
        self.assertEqual(pushes, ["git push origin HEAD:refs/heads/test"])
        self.assertNotRegex(self.wf, r"--force|\s-f\b|\+refs|refs/heads/pages|origin pages|worktree|merge|rm -rf")
        self.assertIn("git add -- site/guide.xml site/status.json site/_headers", self.wf)
        self.assertNotRegex(self.wf, r"git add (-A|\.|--all)")
        # every git write comes after the audit step
        self.assertLess(self.wf.index("Audit output"), self.wf.index("git add"))


class FeedFailureTest(Base):
    def test_malformed_feed(self):
        self.write_feeds(b"<tv><channel id='ONE.uk'>", feed_xml({"TWO.dk": schedule()}))
        self.assert_fails_unchanged("parse error")

    def test_bad_gzip(self):
        self.write_feeds(feed_xml({"ONE.uk": schedule()}), b"")
        self.dk.write_bytes(b"\x1f\x8bnot really gzip")
        self.assert_fails_unchanged("gzip")

    def test_missing_channel(self):
        self.write_feeds(feed_xml({"NOPE.uk": schedule()}), feed_xml({"TWO.dk": schedule()}))
        self.assert_fails_unchanged("ONE.uk")

    def test_stale_feed(self):
        old = schedule(start=NOW - timedelta(days=3), hours=48)
        self.write_feeds(feed_xml({"ONE.uk": old}), feed_xml({"TWO.dk": schedule()}))
        self.assert_fails_unchanged("one.test: no programme airing now")

    def test_short_horizon(self):
        self.write_feeds(feed_xml({"ONE.uk": schedule(hours=20)}), feed_xml({"TWO.dk": schedule()}))
        self.assert_fails_unchanged("ends before now\\+24h")

    def test_doctype_and_entities_rejected(self):
        evil = feed_xml({"ONE.uk": schedule()}, prolog='<!DOCTYPE tv [<!ENTITY x SYSTEM "file:///etc/passwd">]>')
        self.write_feeds(evil, feed_xml({"TWO.dk": schedule()}))
        self.assert_fails_unchanged("DOCTYPE")

    def test_naive_timestamp_rejected(self):
        self.write_feeds(feed_xml({"ONE.uk": schedule()}, tz=""), feed_xml({"TWO.dk": schedule()}))
        self.assert_fails_unchanged("timezone")

    def test_empty_title(self):
        progs = schedule(); s, e, _ = progs[12]; progs[12] = (s, e, "  ")
        self.write_feeds(feed_xml({"ONE.uk": progs}), feed_xml({"TWO.dk": schedule()}))
        self.assert_fails_unchanged("empty title")

    def test_gap(self):
        progs = [p for p in schedule() if not (NOW + timedelta(hours=3) <= p[0] < NOW + timedelta(hours=5))]
        self.write_feeds(feed_xml({"ONE.uk": progs}), feed_xml({"TWO.dk": schedule()}))
        self.assert_fails_unchanged("gap")

    def test_overlap(self):
        progs = schedule(); s, e, t = progs[14]; progs[14] = (s, e + timedelta(minutes=40), t)
        self.write_feeds(feed_xml({"ONE.uk": progs}), feed_xml({"TWO.dk": schedule()}))
        self.assert_fails_unchanged("overlap")

    def test_negative_duration(self):
        progs = schedule(); s, e, t = progs[14]; progs[14] = (e, s, t)
        self.write_feeds(feed_xml({"ONE.uk": progs}), feed_xml({"TWO.dk": schedule()}))
        self.assert_fails_unchanged("non-positive duration")

    def test_duplicate_programmes(self):
        progs = schedule(); progs.insert(14, progs[14])
        self.write_feeds(feed_xml({"ONE.uk": progs}), feed_xml({"TWO.dk": schedule()}))
        self.assert_fails_unchanged("duplicate")

    def test_decompressed_size_bound(self):
        with mock.patch.object(bg, "MAX_DECOMPRESSED", 1000):
            self.assert_fails_unchanged("size limit")

    def test_missing_offline_file(self):
        self.dk.unlink()
        self.assert_fails_unchanged("cannot read offline feed")


class OutputGuardTest(Base):
    def test_refuses_existing_outputs_and_serve(self):
        root = self.dir / "project"
        root.mkdir()
        (root / "channels.json").write_text("{}", encoding="utf-8")
        for target in (root / "guide.xml", self.dir / "serve" / "guide.xml", self.dir / "PLAYLIST.M3U",
                       self.dir / "x.m3u8"):
            with self.subTest(target=target):
                with self.assertRaisesRegex(bg.BuildError, "refusing"):
                    bg.build(self.mapping, target, None, {"UK1": self.uk, "DK1": self.dk}, False, NOW)
                self.assertFalse(target.exists())


class NetworkPolicyTest(unittest.TestCase):
    def test_allowlist(self):
        for url in bg.ALLOWED_SOURCES.values():
            bg.check_url(url)
        for url in (
            "http://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz",
            "https://epgshare01.online.evil.test/epgshare01/epg_ripper_UK1.xml.gz",
            "https://epgshare01.online/epgshare01/epg_ripper_US1.xml.gz",
            "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz?x=1",
            "https://example.test/guide.xml",
        ):
            with self.subTest(url=url), self.assertRaises(bg.BuildError):
                bg.check_url(url)

    def test_disallowed_url_never_opens_connection(self):
        with mock.patch("urllib.request.build_opener") as opener:
            with self.assertRaises(bg.BuildError):
                bg.fetch_source("https://example.test/x.xml.gz")
            opener.assert_not_called()

    def test_redirects_refused(self):
        with self.assertRaisesRegex(bg.BuildError, "redirect refused"):
            bg._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test/")

    def test_fetch_uses_browser_user_agent_and_bounds(self):
        seen = {}

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def geturl(self): return bg.ALLOWED_SOURCES["UK1"]
            def read(self, n): seen["n"] = n; return b"x" * 10

        class Opener:
            def open(self, req, timeout):
                seen["ua"] = req.get_header("User-agent"); seen["timeout"] = timeout
                return Resp()

        with mock.patch("urllib.request.build_opener", return_value=Opener()):
            self.assertEqual(bg.fetch_source(bg.ALLOWED_SOURCES["UK1"]), b"x" * 10)
        self.assertTrue(seen["ua"].startswith("Mozilla/5.0"))
        self.assertEqual(seen["n"], bg.MAX_COMPRESSED + 1)

    def test_oversize_download_rejected(self):
        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def geturl(self): return bg.ALLOWED_SOURCES["DK1"]
            def read(self, n): return b"x" * n

        class Opener:
            def open(self, req, timeout): return Resp()

        with mock.patch.object(bg, "MAX_COMPRESSED", 100), \
                mock.patch("urllib.request.build_opener", return_value=Opener()):
            with self.assertRaisesRegex(bg.BuildError, "size limit"):
                bg.fetch_source(bg.ALLOWED_SOURCES["DK1"])


class CliTest(Base):
    def test_cli_exit_codes(self):
        args = ["--mapping", str(self.mapping), "--output", str(self.out), "--now", NOW.isoformat(),
                "--feed", f"UK1={self.uk}", "--feed", f"DK1={self.dk}"]
        self.assertEqual(bg.main(args + ["--report", str(self.report)]), 0)
        good = self.out.read_bytes()
        self.write_mapping(mapping(("provisional", "verified")))
        self.assertEqual(bg.main(args), 1)
        self.assertEqual(self.out.read_bytes(), good)
        self.assertEqual(bg.main(args + ["--feed", "XX=1"]), 2)
        self.assertEqual(bg.main(["--output", str(self.out), "--now", "2030-01-15T12:00:00"]), 2)


if __name__ == "__main__":
    unittest.main()
