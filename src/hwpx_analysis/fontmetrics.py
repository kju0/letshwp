"""TrueType 글꼴 파일(.ttf/.ttc)에서 글자 폭(advance width)을 읽는다. 표준 라이브러리만 사용한다.

근거: OpenType 규격의 sfnt 테이블 구조 — head(unitsPerEm), hhea(numberOfHMetrics), hmtx(advance), cmap(format 4/12), name(글꼴 이름).
내어쓰기 폭을 한글과 같은 기준(글꼴의 실제 글자 폭)으로 계산하기 위해 쓴다.

글꼴 파일은 라이선스 파일이므로 저장소에 넣지 않고 글꼴 폴더(기본 <저장소>/fonts, 환경 변수 LETSHWP_FONT_DIR)에 둔다.
"""

import os
import struct
from pathlib import Path


class FontError(RuntimeError):
    pass


class TrueTypeFont:
    """글꼴 하나(.ttc 는 안의 n번째 글꼴)의 cmap + hmtx 를 읽어 글자 폭(em 단위)을 돌려준다."""

    def __init__(self, path, index=0):
        self.path = str(path)
        with open(path, "rb") as f:
            self.data = f.read()
        d = self.data
        off = 0
        if d[:4] == b"ttcf":
            n = struct.unpack(">I", d[8:12])[0]
            if index >= n:
                raise FontError("%s: TTC 안에 글꼴 %d번이 없음(%d개)" % (path, index, n))
            off = struct.unpack(">I", d[12 + 4 * index:16 + 4 * index])[0]
        elif d[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
            raise FontError("%s: TrueType/OpenType 파일이 아님" % path)
        num = struct.unpack(">H", d[off + 4:off + 6])[0]
        self.tables = {}
        for i in range(num):
            tag, _, o, ln = struct.unpack(">4sIII", d[off + 12 + 16 * i:off + 28 + 16 * i])
            self.tables[tag.decode("latin-1")] = (o, ln)
        for t in ("head", "hhea", "hmtx", "cmap", "name"):
            if t not in self.tables:
                raise FontError("%s: %s 테이블이 없음" % (path, t))
        self.units_per_em = struct.unpack(">H", d[self.tables["head"][0] + 18:self.tables["head"][0] + 20])[0]
        self.num_h_metrics = struct.unpack(">H", d[self.tables["hhea"][0] + 34:self.tables["hhea"][0] + 36])[0]
        self.names = self._read_names()
        self._cmap = self._read_cmap()
        self._cache = {}

    def _read_names(self):
        d = self.data
        o, _ = self.tables["name"]
        cnt, so = struct.unpack(">HH", d[o + 2:o + 6])
        names = set()
        for i in range(cnt):
            pid, eid, lid, nid, ln, noff = struct.unpack(">HHHHHH", d[o + 6 + 12 * i:o + 18 + 12 * i])
            if nid not in (1, 4, 16):   # family, full name, typographic family
                continue
            raw = d[o + so + noff:o + so + noff + ln]
            try:
                s = raw.decode("utf-16-be") if pid in (0, 3) else raw.decode("mac-roman", "replace")
            except UnicodeDecodeError:
                continue
            names.add(s.strip())
        return names

    def _read_cmap(self):
        """(platform 3, encoding 1 또는 10) / (0, x) 의 format 4·12 서브테이블 → 코드포인트 조회 함수 목록."""
        d = self.data
        o, _ = self.tables["cmap"]
        n = struct.unpack(">H", d[o + 2:o + 4])[0]
        subs = []
        for i in range(n):
            pid, eid, so = struct.unpack(">HHI", d[o + 4 + 8 * i:o + 12 + 8 * i])
            if pid not in (0, 3):
                continue
            base = o + so
            fmt = struct.unpack(">H", d[base:base + 2])[0]
            if fmt == 4:
                subs.append((0 if (pid, eid) == (3, 1) else 1, self._format4(base)))
            elif fmt == 12:
                subs.append((2, self._format12(base)))
        if not subs:
            raise FontError("%s: 유니코드 cmap(format 4/12)이 없음" % self.path)
        subs.sort(key=lambda x: x[0])
        return [fn for _, fn in subs]

    def _format4(self, base):
        d = self.data
        seg_x2 = struct.unpack(">H", d[base + 6:base + 8])[0]
        seg = seg_x2 // 2
        ends = struct.unpack(">%dH" % seg, d[base + 14:base + 14 + seg_x2])
        starts = struct.unpack(">%dH" % seg, d[base + 16 + seg_x2:base + 16 + 2 * seg_x2])
        deltas = struct.unpack(">%dh" % seg, d[base + 16 + 2 * seg_x2:base + 16 + 3 * seg_x2])
        ro_off = base + 16 + 3 * seg_x2
        ros = struct.unpack(">%dH" % seg, d[ro_off:ro_off + seg_x2])

        def lookup(cp):
            if cp > 0xFFFF:
                return 0
            for i in range(seg):
                if starts[i] <= cp <= ends[i]:
                    if ros[i] == 0:
                        return (cp + deltas[i]) & 0xFFFF
                    a = ro_off + 2 * i + ros[i] + 2 * (cp - starts[i])
                    g = struct.unpack(">H", d[a:a + 2])[0]
                    return (g + deltas[i]) & 0xFFFF if g else 0
            return 0
        return lookup

    def _format12(self, base):
        d = self.data
        ngroups = struct.unpack(">I", d[base + 12:base + 16])[0]
        groups = [struct.unpack(">III", d[base + 16 + 12 * i:base + 28 + 12 * i]) for i in range(ngroups)]

        def lookup(cp):
            for s, e, g in groups:
                if s <= cp <= e:
                    return g + (cp - s)
            return 0
        return lookup

    def glyph_id(self, ch):
        cp = ord(ch)
        for fn in self._cmap:
            g = fn(cp)
            if g:
                return g
        return 0

    def advance_em(self, ch):
        """글자의 가로 폭(em). 글꼴에 글자가 없으면 None."""
        if ch in self._cache:
            return self._cache[ch]
        g = self.glyph_id(ch)
        if g == 0:
            self._cache[ch] = None
            return None
        o, _ = self.tables["hmtx"]
        idx = min(g, self.num_h_metrics - 1)
        adv = struct.unpack(">H", self.data[o + 4 * idx:o + 4 * idx + 2])[0]
        val = adv / float(self.units_per_em)
        self._cache[ch] = val
        return val


def default_font_dir(root):
    return Path(os.environ.get("LETSHWP_FONT_DIR") or (Path(root) / "fonts"))


class FontRegistry:
    """글꼴 폴더의 .ttf/.ttc 를 읽어 글꼴 이름(한글에서 쓰는 face 이름) → TrueTypeFont 로 찾는다."""

    def __init__(self, font_dir=None):
        self.dir = Path(font_dir) if font_dir else None
        self.fonts = {}      # 정규화한 이름 -> TrueTypeFont
        self.files = []      # 읽은 파일 목록
        self.errors = []
        if self.dir and self.dir.is_dir():
            for p in sorted(self.dir.iterdir()):
                if p.suffix.lower() not in (".ttf", ".ttc", ".otf"):
                    continue
                self._load(p)

    @staticmethod
    def _norm(name):
        return (name or "").replace(" ", "").lower()

    def _load(self, path):
        try:
            with open(path, "rb") as f:
                head = f.read(16)
            count = struct.unpack(">I", head[8:12])[0] if head[:4] == b"ttcf" else 1
            for i in range(count):
                font = TrueTypeFont(path, i)
                self.files.append("%s%s" % (path.name, "#%d" % i if count > 1 else ""))
                for n in font.names:
                    self.fonts.setdefault(self._norm(n), font)
                self.fonts.setdefault(self._norm(path.stem), font)
        except (FontError, OSError, struct.error) as e:
            self.errors.append("%s: %s" % (path.name, e))

    def get(self, face):
        return self.fonts.get(self._norm(face))

    def available(self):
        return sorted({f.path for f in self.fonts.values()})
