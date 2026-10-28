"""글꼴 폭 읽기(src/hwpx_analysis/fontmetrics.py) 테스트. 합성 TrueType 파일을 만들어 글꼴 파일 없이도 검사한다."""

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hwpx_analysis.fontmetrics import TrueTypeFont, FontRegistry, FontError  # noqa: E402

UPEM = 1000
# 글리프: 0 .notdef(500), 1 공백(250), 2 '가'(1000), 3 '□'(1000), 4 '1'(583), 5 '.'(208)
GLYPHS = {" ": 1, "가": 2, "□": 3, "1": 4, ".": 5}
ADV = [500, 250, 1000, 1000, 583, 208]


def _cmap4():
    # 각 글자를 1개 세그먼트로(idDelta 사용), 마지막 0xFFFF 세그먼트
    chars = sorted(GLYPHS, key=ord)
    segs = [(ord(c), ord(c), (GLYPHS[c] - ord(c)) & 0xFFFF) for c in chars] + [(0xFFFF, 0xFFFF, 1)]
    n = len(segs)
    body = struct.pack(">%dH" % n, *[e for _, e, _ in segs]) + b"\x00\x00"
    body += struct.pack(">%dH" % n, *[s for s, _, _ in segs])
    body += struct.pack(">%dH" % n, *[d for _, _, d in segs])
    body += struct.pack(">%dH" % n, *([0] * n))
    sub = struct.pack(">HHHHHHH", 4, 14 + len(body), 0, n * 2, 0, 0, 0) + body
    return struct.pack(">HH", 0, 1) + struct.pack(">HHI", 3, 1, 12) + sub


def _name(family):
    s = family.encode("utf-16-be")
    rec = struct.pack(">HHHHHH", 3, 1, 0x412, 1, len(s), 0)
    return struct.pack(">HHH", 0, 1, 6 + 12) + rec + s


def build_ttf(path, family="테스트명조"):
    tables = {
        b"head": b"\x00" * 18 + struct.pack(">H", UPEM) + b"\x00" * 34,
        b"hhea": b"\x00" * 34 + struct.pack(">H", len(ADV)),
        b"hmtx": b"".join(struct.pack(">Hh", a, 0) for a in ADV),
        b"cmap": _cmap4(),
        b"name": _name(family),
    }
    n = len(tables)
    off = 12 + 16 * n
    dir_, data = b"", b""
    for tag, blob in tables.items():
        dir_ += struct.pack(">4sIII", tag, 0, off + len(data), len(blob))
        pad = blob + b"\x00" * (-len(blob) % 4)
        data += pad
    Path(path).write_bytes(struct.pack(">IHHHH", 0x00010000, n, 0, 0, 0) + dir_ + data)
    return path


class TrueTypeFontTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = build_ttf(Path(self.tmp.name) / "test.ttf")

    def tearDown(self):
        self.tmp.cleanup()

    def test_advance_widths(self):
        f = TrueTypeFont(self.path)
        self.assertEqual(f.units_per_em, UPEM)
        self.assertIn("테스트명조", f.names)
        self.assertEqual(f.advance_em(" "), 0.25)
        self.assertEqual(f.advance_em("가"), 1.0)
        self.assertEqual(f.advance_em("1"), 0.583)
        self.assertEqual(f.advance_em("."), 0.208)
        self.assertIsNone(f.advance_em("※"))        # 글꼴에 없는 글자

    def test_registry_lookup_by_name_and_filename(self):
        reg = FontRegistry(self.tmp.name)
        self.assertEqual(reg.files, ["test.ttf"])
        self.assertIs(reg.get("테스트명조"), reg.get("테스트 명조"))   # 공백·대소문자 무시
        self.assertIsNotNone(reg.get("test"))
        self.assertIsNone(reg.get("휴먼명조"))
        self.assertEqual(FontRegistry("/nonexistent-dir").files, [])

    def test_invalid_file(self):
        bad = Path(self.tmp.name) / "bad.ttf"
        bad.write_bytes(b"not a font")
        with self.assertRaises(FontError):
            TrueTypeFont(bad)
        reg = FontRegistry(self.tmp.name)
        self.assertTrue(any("bad.ttf" in e for e in reg.errors))


class HangingWidthWithFontTest(unittest.TestCase):
    """글꼴 폭으로 내어쓰기 폭을 계산하는 흐름."""

    def test_width_from_font(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_spacing_rules import P, inspection
        import copy
        from checker import check_document, load_config
        cfg, cp = load_config()
        cfg = copy.deepcopy(cfg)
        with tempfile.TemporaryDirectory() as d:
            build_ttf(Path(d) / "a.ttf", family="휴먼명조")
            cfg["rules"]["HANGING_INDENT"]["font_dir"] = d
            res = check_document(inspection([P("□ 항목", size=14), P("1. 번호", size=16), P("※ 주석", size=14)]), cfg, cp)
        rows = {r["target"]["paragraph_index"]: r for r in res["results"]
                if r["rule_id"] == "HANGING_INDENT" and r.get("sub_item") == "둘째 줄 정렬"}
        self.assertEqual(rows[0]["expected"]["value"]["indent_pt"], -(14 * 1.0 + 14 * 0.25))        # □ + 공백
        self.assertFalse(rows[0]["expected"]["value"]["approximated"])
        self.assertEqual(rows[1]["expected"]["value"]["indent_pt"], -round(16 * (0.583 + 0.208 + 0.25), 2))
        # 글꼴에 없는 ※ 는 근사(1칸)로 대신하고 표시한다
        self.assertEqual(rows[2]["expected"]["value"]["indent_pt"], -(14 + 14 * 0.25))
        self.assertTrue(rows[2]["expected"]["value"]["approximated"])
        self.assertIn("글꼴에 글자 없음", rows[2]["observation"])
        summary = [r for r in res["results"] if r.get("sub_item") == "글꼴 폭"]
        self.assertEqual([r["status"] for r in summary], ["PASS"])


@unittest.skipUnless((ROOT / "fonts").is_dir() and any((ROOT / "fonts").glob("*.ttf")), "fonts/ 폴더에 글꼴 없음")
class RealFontTest(unittest.TestCase):
    def test_real_fonts_readable(self):
        reg = FontRegistry(ROOT / "fonts")
        self.assertEqual(reg.errors, [])
        for face in ("휴먼명조", "HY헤드라인M"):
            f = reg.get(face)
            if f:
                self.assertEqual(f.advance_em("가"), 1.0)
                self.assertIsNotNone(f.advance_em(" "))


if __name__ == "__main__":
    unittest.main()
