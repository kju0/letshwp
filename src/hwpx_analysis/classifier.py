"""문단 역할 분류기 (규칙 기반, 근거·신뢰도 포함).

설계 원칙
- 각 규칙은 (역할, 가중치 w, 근거 설명)을 남긴다. 가중치는 0~1 이며 음수는 반대 근거다.
- 역할 점수 = 1 - Π(1 - w⁺) 에 반대 근거 (1 + w⁻) 를 곱한다.
  이는 "독립적인 근거가 쌓일수록 확신이 커진다"는 직관을 따르는 휴리스틱 점수이며
  통계적으로 보정된 확률이 아니다.
- 최고 점수가 ACCEPT_THRESHOLD 미만이거나 2위와의 차이가 MIN_MARGIN 미만이면
  '미분류(unclassified)'로 표시하고 후보와 근거를 함께 남긴다.
- 구조적 문맥(머리말/꼬리말, 각주, 캡션 요소, 표 셀)은 XML 구조에서 확정되는 사실이므로
  높은 가중치를 갖는다. 글자 크기·굵기·번호 패턴은 문서 내 상대 비교로 판단한다.
- 대·중·소 제목 수준은 절대 기준이 아니라 문서 내 제목 후보 그룹의 상대 순위로 정한다.

규칙과 가중치는 RULES 에 모여 있어 조정·추가·제거가 쉽다.
"""

import difflib
import re
from collections import Counter, defaultdict

ROLE_LABELS = {
    "document_title": "문서 제목",
    "heading_1": "대제목",
    "heading_2": "중제목",
    "heading_3": "소제목",
    "body": "본문",
    "table_title": "표 제목",
    "figure_caption": "그림 캡션",
    "note": "참고·주석",
    "table_cell": "표 내부 텍스트",
    "header_footer": "머리말·꼬리말",
    "toc": "차례",
    "object_anchor": "개체 배치 문단",
    "empty": "빈 문단",
    "unclassified": "미분류",
}

ACCEPT_THRESHOLD = 0.6
MIN_MARGIN = 0.15
TOP_REGION_SIZE = 5          # 문서 앞부분으로 간주할 비어있지 않은 문단 수
MAX_HEADING_LEVELS = 3       # 대·중·소
SECONDARY_BODY_SHARE = 0.18  # 보조 본문 크기로 인정할 최소 글자 비율

RULES = {
    # 구조 문맥
    "ctx_header_footer": 0.99,
    "ctx_footnote": 0.95,
    "ctx_caption": 0.97,
    "ctx_table_cell": 0.90,
    # 제목(heading)
    "h_outline": 0.55,
    "h_style_name": 0.20,       # 스타일 이름은 실제로 오용되는 사례가 관측되어 낮게 둔다
    "h_num_major": 0.45,        # Ⅰ. / 제1장
    "h_num_mid": 0.35,          # 1. / 가.
    "h_num_minor": 0.10,        # 1) (1) ① ...
    "h_size_large": 0.45,       # 본문보다 2pt 이상 큼
    "h_size_medium": 0.25,      # 본문보다 1pt 이상 큼
    "h_bold": 0.30,
    "h_heading_font": 0.30,
    "h_other_font": 0.10,
    "h_short": 0.20,
    "h_long": -0.60,
    "h_sentence_end": -0.35,
    "h_title_like": -0.40,      # 번호 없이 문서 최대 크기이며 문서 앞부분 -> 문서 제목 쪽 근거
    # 본문
    "b_size_equal": 0.35,
    "b_size_secondary": 0.25,
    "b_font_equal": 0.15,
    "b_long": 0.30,
    "b_sentence_end": 0.30,
    "b_bullet": 0.45,
    "b_minor_number": 0.30,
    "b_short_plain": -0.30,
    "b_emphasized": -0.30,
    "b_marked_other": -0.50,    # 주석·캡션 표지가 있는 문단
    # 참고·주석
    "n_marker_strong": 0.75,    # ※
    "n_marker": 0.60,           # 주), 참고:, 출처: ...
    "n_marker_weak": 0.45,      # [... 참고]
    "n_smaller": 0.20,
    # 캡션
    "c_pattern": 0.65,
    "c_adjacent": 0.25,
    "c_center": 0.10,
    "c_long": -0.50,
    # 문서 제목
    "t_max_size": 0.45,
    "t_large": 0.30,
    "t_top": 0.20,
    "t_center": 0.15,
    "t_title_box": 0.25,
    "t_textbox": 0.15,
    # 한글은 문서 정보의 제목을 첫 줄로 채워 저장하는 경우가 관측되어(샘플 s2/s3) 단독 근거로 약하게 둔다.
    "t_metadata": 0.30,
    "t_label": 0.50,
    "t_style_name": 0.25,
    "t_long": -0.60,
    # 차례
    "toc_page_ref": 0.60,
    "toc_style": 0.30,
    "toc_heading": 0.60,
}

HEADING_FONT_HINTS = ("헤드라인", "고딕", "Gothic", "견출", "돋움")
SERIF_HINTS = ("명조", "바탕", "Myeongjo", "Batang", "궁서")

_HANGUL_SEQ = "가나다라마바사아자차카타파하"
NUM_PATTERNS = [
    # (kind, regex, rank)  rank 가 작을수록 상위
    ("roman", re.compile(r"^([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]|[IVX]{1,4})\s*[.．]"), 0),
    ("chapter", re.compile(r"^제\s*\d+\s*[장절편부]"), 0),
    ("digit_dot", re.compile(r"^\d{1,2}\s*[.．](?!\d)"), 1),
    ("hangul_dot", re.compile(r"^[%s]\s*[.．]\s" % _HANGUL_SEQ), 2),
    ("digit_paren", re.compile(r"^\d{1,2}\s*[)）]"), 3),
    ("hangul_paren", re.compile(r"^[%s]\s*[)）]" % _HANGUL_SEQ), 4),
    ("paren_digit", re.compile(r"^[(（]\s*\d{1,2}\s*[)）]"), 5),
    ("paren_hangul", re.compile(r"^[(（]\s*[%s]\s*[)）]" % _HANGUL_SEQ), 6),
    ("circled_digit", re.compile(r"^[①-⑳]"), 7),
    ("circled_hangul", re.compile(r"^[㉮-㉻]"), 8),
]
BULLET_PATTERN = re.compile(r"^([□■◆◇○●◎◦•∙·‧ㆍ▶▷►▪▫☐❍]|ㅇ\s|[-–—]\s)")
NOTE_STRONG = re.compile(r"^※")
NOTE_MARKER = re.compile(r"^(\*|＊|주\s*[)）:：]|주\s*\d+\s*[)）]|참고\s*[:：)]|출처\s*[:：]|자료\s*[:：]|비고\s*[:：]|"
                         r"note\s*[:：]|source\s*[:：]|☞)", re.I)
NOTE_WEAK = re.compile(r"참고\s*[\]）)]\s*$")
FIG_CAPTION = re.compile(r"^[<\[〈《【(]?\s*(그림|사진|도표|Figure|Fig\.?)\s*[0-9]+(\s*[-.]\s*[0-9]+)?\s*[>\]〉》】).:：\s]", re.I)
TBL_CAPTION = re.compile(r"^[<\[〈《【(]?\s*(표|Table)\s*[0-9]+(\s*[-.]\s*[0-9]+)?\s*[>\]〉》】).:：\s]", re.I)
SENTENCE_END = re.compile(r"([.。!?！？]|[다요음함임됨])\s*[)\]]?\s*$")
TOC_PAGE_REF = re.compile(r"\t\s*-?\s*\d{1,4}\s*-?\s*$")
TOC_HEADING = re.compile(r"^\s*(목\s*차|차\s*례|contents|table of contents)\s*$", re.I)
TITLE_LABEL = re.compile(r"^(문서\s*)?제\s*목\s*[:：]")
HEADING_STYLE = re.compile(r"(개요|제목|heading|outline)\s*\d", re.I)
TITLE_STYLE = re.compile(r"^(제목|title|표제)$", re.I)
TOC_STYLE = re.compile(r"(차례|목차|toc)", re.I)


def detect_list_marker(text):
    s = text.lstrip()
    for kind, rx, rank in NUM_PATTERNS:
        m = rx.match(s)
        if m:
            return {"kind": kind, "marker": m.group(0).strip(), "rank": rank, "type": "number"}
    m = BULLET_PATTERN.match(s)
    if m:
        return {"kind": "bullet", "marker": m.group(0).strip(), "rank": None, "type": "bullet"}
    return None


def _combine(evidence):
    pos = [e["weight"] for e in evidence if e["weight"] > 0]
    neg = [e["weight"] for e in evidence if e["weight"] < 0]
    p = 1.0
    for w in pos:
        p *= (1 - w)
    score = 1 - p if pos else 0.0
    for w in neg:
        score *= (1 + w)
    return round(max(0.0, min(1.0, score)), 3)


def _mode_weighted(pairs):
    c = Counter()
    for k, w in pairs:
        if k is not None:
            c[k] += w
    return c.most_common(1)[0][0] if c else None


class RoleClassifier:
    def __init__(self, paragraphs, tables, pictures, shapes, metadata=None):
        self.paras = paragraphs
        self.tables = {t["id"]: t for t in tables}
        self.pictures = {p["id"]: p for p in pictures}
        self.shapes = {s["id"]: s for s in shapes}
        self.meta_title = ((metadata or {}).get("title") or "").strip()
        self.baseline = {}

    # ------------------------------------------------------------------
    def run(self):
        self._compute_baseline()
        self._index_siblings()
        candidates = {}
        for p in self.paras:
            p["list_marker"] = detect_list_marker(p.get("text") or "")
            candidates[p["index"]] = self._score(p)
        self._decide(candidates)
        self._assign_heading_levels()
        return {"baseline": self.baseline, "thresholds": {
            "accept": ACCEPT_THRESHOLD, "min_margin": MIN_MARGIN}}

    # ------------------------------------------------------------------
    def _size(self, p):
        cf = p.get("char_format") or {}
        return cf.get("effective_size_pt_hangul") or cf.get("size_pt")

    def _compute_baseline(self):
        """본문 기준: 본문 영역에서 20자 이상인 문단의 글자 수 가중 최빈 크기/글꼴."""
        body = [p for p in self.paras if p["context"]["container"] == "body" and p["visible_length"] >= 20]
        src = "본문 영역 20자 이상 문단"
        if not body:
            body = [p for p in self.paras if not p["is_empty"]]
            src = "전체 비어있지 않은 문단(본문 영역 장문 없음)"
        size = _mode_weighted((self._size(p), p["visible_length"]) for p in body)
        font = _mode_weighted(((p.get("char_format") or {}).get("font_hangul"), p["visible_length"]) for p in body)
        bold = _mode_weighted(((p.get("char_format") or {}).get("bold"), p["visible_length"]) for p in body)
        content = [p for p in self.paras if not p["is_empty"]
                   and p["context"]["container"] not in ("header", "footer", "footnote", "endnote", "memo")]
        sizes = [self._size(p) for p in content if self._size(p)]
        order = [p["index"] for p in content if p["context"]["container"] in ("body", "textbox", "table_cell")]
        size_chars = Counter()
        for p in body:
            if self._size(p):
                size_chars[self._size(p)] += p["visible_length"]
        total = sum(size_chars.values()) or 1
        share = {k: round(v / total, 3) for k, v in size_chars.most_common()}
        # 본문 크기가 섞인 문서: 장문 본문 글자의 SECONDARY_BODY_SHARE 이상을 차지하는 크기는 보조 본문 크기로 인정
        secondary = sorted(k for k, v in share.items() if k != size and v >= SECONDARY_BODY_SHARE)
        self.baseline = {
            "body_size_pt": size, "body_font": font, "body_bold": bool(bold),
            "body_size_share": share.get(size),
            "secondary_body_sizes_pt": secondary,
            "body_size_distribution": {str(k): v for k, v in list(share.items())[:6]},
            "max_size_pt": max(sizes) if sizes else None,
            "source": src, "sample_paragraphs": len(body),
        }
        self._top_region = set(order[:TOP_REGION_SIZE])

    def _index_siblings(self):
        groups = defaultdict(list)
        for p in self.paras:
            parent = p["path"].rsplit("/", 1)[0]
            groups[parent].append(p["index"])
        self._siblings = {}
        for parent, idxs in groups.items():
            for pos, i in enumerate(idxs):
                self._siblings[i] = (idxs, pos)

    def _neighbor_objects(self, p):
        """같은 컨테이너에서 바로 앞·뒤의 비어있지 않은(또는 개체를 가진) 문단이 가진 개체 종류."""
        idxs, pos = self._siblings[p["index"]]
        found = []
        for direction, rng in (("prev", range(pos - 1, -1, -1)), ("next", range(pos + 1, len(idxs)))):
            for j in rng:
                q = self.paras[idxs[j]]
                kinds = self._object_kinds(q)
                if kinds:
                    found.extend((direction, k) for k in kinds)
                    break
                if not q["is_empty"]:
                    break
        own = [("self", k) for k in self._object_kinds(p)]
        return found + own

    def _object_kinds(self, p):
        kinds = set()
        for oid in p.get("objects") or []:
            if oid in self.tables:
                kinds.add("table")
            elif oid in self.pictures:
                kinds.add("picture")
            elif oid in self.shapes:
                s = self.shapes[oid]
                if s["kind"] == "container" or not s.get("text_paragraphs"):
                    kinds.add("picture")
        return kinds

    # ------------------------------------------------------------------
    def _score(self, p):
        ev = defaultdict(list)

        def add(role, rule, detail, weight=None):
            ev[role].append({"rule": rule, "weight": RULES[rule] if weight is None else weight, "detail": detail})

        ctx = p["context"]["container"]
        text = (p.get("text") or "").strip()
        if p["is_empty"]:
            if p.get("objects"):
                add("object_anchor", "ctx_caption", "텍스트 없이 개체(%s)만 배치된 문단" % ",".join(p["objects"]), 0.95)
            else:
                add("empty", "ctx_caption", "공백 외 텍스트 없음", 1.0)
            return ev

        if ctx in ("header", "footer"):
            add("header_footer", "ctx_header_footer", "머리말/꼬리말(hp:%s) 영역 안의 문단" % ctx)
            return ev
        if ctx in ("footnote", "endnote"):
            add("note", "ctx_footnote", "각주/미주(hp:%s) 영역 안의 문단" % ("footNote" if ctx == "footnote" else "endNote"))
            return ev
        if ctx == "caption":
            of = p["context"].get("caption_of")
            role = "table_title" if of == "table" else "figure_caption"
            add(role, "ctx_caption", "hp:caption 요소 안의 문단 (대상: %s)" % of)
            return ev

        base = self.baseline
        size = self._size(p)
        cf = p.get("char_format") or {}
        pf = p.get("para_format") or {}
        font = cf.get("font_hangul")
        bold = bool(cf.get("bold"))
        n = p["visible_length"]
        sentence_end = bool(SENTENCE_END.search(text))
        marker = p["list_marker"]
        style_name = (p.get("style") or {}).get("name") or ""
        diff = (size - base["body_size_pt"]) if size and base.get("body_size_pt") else None
        centered = pf.get("align") == "CENTER"
        secondary_body = size in (base.get("secondary_body_sizes_pt") or [])

        # --- 표 셀: 표 내부 텍스트가 기본. 제목 박스만 예외로 문서 제목 후보 ---
        if ctx == "table_cell":
            add("table_cell", "ctx_table_cell", "표 %s 셀%s 안의 문단" % (p["context"]["table_id"], p["context"]["cell"]))
            self._title_rules(p, add, size, diff, centered, n, style_name, text, in_table=True)
            return ev

        # --- 차례 ---
        if TOC_PAGE_REF.search(p.get("text") or ""):
            add("toc", "toc_page_ref", "탭 뒤에 쪽 번호로 끝남(차례 항목 형태)")
        if TOC_STYLE.search(style_name):
            add("toc", "toc_style", "스타일 이름 '%s'" % style_name)
        if TOC_HEADING.match(text):
            add("toc", "toc_heading", "차례 제목 문구 '%s'" % text)

        # --- 캡션 ---
        neigh = self._neighbor_objects(p)
        for rx, role, obj in ((FIG_CAPTION, "figure_caption", "picture"), (TBL_CAPTION, "table_title", "table")):
            if rx.match(text):
                add(role, "c_pattern", "캡션 번호 패턴 '%s'" % rx.match(text).group(0).strip())
                adj = [d for d, k in neigh if k == obj]
                if adj:
                    add(role, "c_adjacent", "인접 문단(%s)에 %s 개체" % ("/".join(adj), "그림" if obj == "picture" else "표"))
                if centered:
                    add(role, "c_center", "가운데 정렬")
                if n > 100:
                    add(role, "c_long", "100자 초과 (%d자)" % n)

        # --- 참고·주석 ---
        if NOTE_STRONG.match(text):
            add("note", "n_marker_strong", "'※'로 시작")
        elif NOTE_MARKER.match(text):
            add("note", "n_marker", "주석 표지 '%s'로 시작" % NOTE_MARKER.match(text).group(0))
        elif NOTE_WEAK.search(text):
            add("note", "n_marker_weak", "'참고'로 끝나는 괄호 표기")
        if ev.get("note") and diff is not None and diff <= -1:
            add("note", "n_smaller", "본문보다 %.1fpt 작음" % -diff)

        # 차례 항목 형태가 확인되면 제목/문서 제목 규칙은 적용하지 않는다(차례 항목은 제목 서식을 흉내 내는 경우가 많음).
        is_toc = bool(ev.get("toc"))

        # --- 문서 제목 ---
        if not is_toc:
            self._title_rules(p, add, size, diff, centered, n, style_name, text, in_table=False)

        # --- 제목(heading) ---
        h = []
        strong = False
        an = p.get("auto_numbering") or {}
        if an.get("type") == "OUTLINE":
            h.append(("h_outline", "개요 문단(OUTLINE) %d수준" % an.get("level", 0), None)); strong = True
        if HEADING_STYLE.search(style_name):
            h.append(("h_style_name", "스타일 이름 '%s'" % style_name, None))
        if marker and marker["type"] == "number":
            if marker["rank"] == 0:
                h.append(("h_num_major", "상위 번호 '%s'" % marker["marker"], None)); strong = True
            elif marker["rank"] <= 2:
                h.append(("h_num_mid", "번호 '%s'" % marker["marker"], None)); strong = True
            else:
                h.append(("h_num_minor", "하위 번호 '%s'" % marker["marker"], None))
        if diff is not None and diff >= 2:
            h.append(("h_size_large", "본문(%.1fpt)보다 %.1fpt 큼" % (base["body_size_pt"], diff), None)); strong = True
        elif diff is not None and diff >= 1:
            h.append(("h_size_medium", "본문(%.1fpt)보다 %.1fpt 큼" % (base["body_size_pt"], diff), None)); strong = True
        if bold and not base.get("body_bold"):
            h.append(("h_bold", "진하게(본문은 보통)", None)); strong = True
        if font and base.get("body_font") and font != base["body_font"]:
            if any(k in font for k in HEADING_FONT_HINTS) and not any(k in base["body_font"] for k in HEADING_FONT_HINTS):
                h.append(("h_heading_font", "제목형 글꼴 '%s' (본문 '%s')" % (font, base["body_font"]), None)); strong = True
            else:
                h.append(("h_other_font", "본문과 다른 글꼴 '%s'" % font, None))
        if n <= 40 and not sentence_end:
            h.append(("h_short", "40자 이하이며 문장 종결 없음", None))
        if n > 80:
            h.append(("h_long", "80자 초과 (%d자)" % n, None))
        if sentence_end:
            h.append(("h_sentence_end", "문장 종결 표현으로 끝남", None))
        if diff is not None and diff >= 1 and secondary_body:
            # 보조 본문 크기와 같으면 크기 근거는 약하게 본다
            h = [x if not x[0].startswith("h_size") else (x[0], x[1] + " (보조 본문 크기와 같아 약화)", 0.1) for x in h]
            strong = any(r in ("h_outline", "h_num_major", "h_num_mid", "h_bold", "h_heading_font") for r, _, _ in h)
        title_like = (not marker and p["index"] in self._top_region and size and base.get("max_size_pt")
                      and size >= base["max_size_pt"] and diff is not None and diff >= 4)
        if title_like:
            h.append(("h_title_like", "번호 없이 문서 최대 크기(%.1fpt)이며 문서 앞부분" % size, None))
        if strong and not marker_is_bullet(marker) and not is_toc:
            for rule, detail, w in h:
                add("heading", rule, detail, w)

        # --- 본문 ---
        if diff is not None and abs(diff) <= 0.5:
            add("body", "b_size_equal", "본문 기준 크기(%.1fpt)와 같음" % base["body_size_pt"])
        elif secondary_body:
            add("body", "b_size_secondary", "보조 본문 크기(%.1fpt, 본문 크기가 섞인 문서)" % size)
        if font and font == base.get("body_font"):
            add("body", "b_font_equal", "본문 기준 글꼴 '%s'" % font)
        if n >= 30:
            add("body", "b_long", "30자 이상 (%d자)" % n)
        if sentence_end:
            add("body", "b_sentence_end", "문장 종결 표현으로 끝남")
        if marker_is_bullet(marker):
            add("body", "b_bullet", "항목 기호 '%s'" % marker["marker"])
        elif marker and marker["type"] == "number" and marker["rank"] >= 3:
            add("body", "b_minor_number", "하위 항목 번호 '%s'" % marker["marker"])
        if n < 30 and not sentence_end and not marker:
            add("body", "b_short_plain", "짧고 문장 종결 없음")
        labelled_title = any(e["rule"] == "t_label" for e in ev.get("document_title", []))
        if ev.get("note") or ev.get("figure_caption") or ev.get("table_title") or labelled_title:
            add("body", "b_marked_other", "주석/캡션/제목 표지가 있어 일반 본문과 구분")
        if (diff is not None and diff >= 2 and not secondary_body) or (bold and not base.get("body_bold")):
            add("body", "b_emphasized", "본문보다 강조됨(크기/굵기)")
        return ev

    def _title_rules(self, p, add, size, diff, centered, n, style_name, text, in_table):
        base = self.baseline
        ctx = p["context"]["container"]
        if in_table:
            t = self.tables.get(p["context"]["table_id"]) or {}
            nonempty = [i for c in t.get("cells", []) for i in c["paragraphs"] if not self.paras[i]["is_empty"]]
            is_box = t.get("cols") == 1 and (t.get("rows") or 0) <= 3 and nonempty == [p["index"]]
            if not is_box:
                return
            add("document_title", "t_title_box", "1열·%d행 표 안의 유일한 텍스트(제목 박스 형태)" % t.get("rows"))
        elif ctx == "textbox" and size and base.get("max_size_pt") and size >= base["max_size_pt"]:
            add("document_title", "t_textbox", "문서 최대 크기 글자를 가진 글상자 문단")
        hit = False
        if size and base.get("max_size_pt") and size >= base["max_size_pt"] and diff is not None and diff >= 4:
            add("document_title", "t_max_size", "문서 최대 글자 크기 %.1fpt" % size); hit = True
        elif diff is not None and diff >= 4:
            add("document_title", "t_large", "본문보다 %.1fpt 큼" % diff); hit = True
        if TITLE_LABEL.match(text):
            add("document_title", "t_label", "'제목:' 표지로 시작"); hit = True
        if self.meta_title:
            body_text = TITLE_LABEL.sub("", text).strip()
            ratio = difflib.SequenceMatcher(None, body_text, self.meta_title).ratio()
            if ratio >= 0.6:
                add("document_title", "t_metadata", "문서 메타데이터(opf:title)와 유사도 %.2f (첫 줄 자동 기록 가능성 있어 약한 근거)" % ratio); hit = True
        if TITLE_STYLE.match(style_name):
            add("document_title", "t_style_name", "스타일 이름 '%s'" % style_name); hit = True
        if hit:
            if p["index"] in self._top_region:
                add("document_title", "t_top", "문서 앞부분(%d번째 이내) 문단" % TOP_REGION_SIZE)
            if centered:
                add("document_title", "t_center", "가운데 정렬")
            if n > 80:
                add("document_title", "t_long", "80자 초과 (%d자)" % n)

    # ------------------------------------------------------------------
    def _decide(self, candidates):
        for p in self.paras:
            ev = candidates[p["index"]]
            scored = sorted(((role, _combine(e)) for role, e in ev.items()), key=lambda x: -x[1])
            cands = [{"role": r, "label": ROLE_LABELS.get(r, "제목(수준 미정)" if r == "heading" else r),
                      "score": s} for r, s in scored]
            top = scored[0] if scored else (None, 0.0)
            second = scored[1][1] if len(scored) > 1 else 0.0
            evidence = [dict(e, role=role) for role, es in ev.items() for e in es]
            # 표 셀 안에서는 표 내부 텍스트가 기본이며, 제목 박스로 확인될 때만 문서 제목으로 본다.
            if "table_cell" in ev:
                dt = dict(scored).get("document_title", 0)
                role, conf = (("document_title", dt) if dt >= ACCEPT_THRESHOLD
                              else ("table_cell", dict(scored)["table_cell"]))
                reason = None
                secondary = [c for c in cands if c["role"] != role and c["score"] >= ACCEPT_THRESHOLD]
            elif top[0] is None or top[1] < ACCEPT_THRESHOLD:
                role, conf, reason = "unclassified", top[1], "최고 점수 %.2f < 기준 %.2f" % (top[1], ACCEPT_THRESHOLD)
                secondary = []
            elif top[1] - second < MIN_MARGIN:
                role, conf = "unclassified", top[1]
                reason = "1·2위 후보 점수 차 %.2f < %.2f (%s vs %s)" % (top[1] - second, MIN_MARGIN, scored[0][0], scored[1][0])
                secondary = []
            else:
                role, conf, reason, secondary = top[0], top[1], None, []
            p["role"] = {
                "role": role,
                "label": ROLE_LABELS.get(role, role),
                "confidence": round(conf, 3),
                "evidence": sorted(evidence, key=lambda e: (e["role"] != role, -abs(e["weight"]))),
                "candidates": cands,
                "unclassified_reason": reason,
                "secondary_roles": [c["role"] for c in secondary] or None,
            }

    def _assign_heading_levels(self):
        """'heading' 으로 판정된 문단을 문서 내 상대 서식으로 그룹화해 대·중·소 수준을 부여한다.
        개요(OUTLINE) 수준이 있으면 그 수준을, 없으면 (글자 크기, 번호 순위, 굵기) 를 기준으로 정렬한다."""
        heads = [p for p in self.paras if p["role"]["role"] == "heading"]
        if not heads:
            return
        groups = defaultdict(list)
        for p in heads:
            groups[self._heading_key(p)].append(p)
        ordered = sorted(groups.keys())
        for rank, key in enumerate(ordered):
            members = groups[key]
            if rank >= MAX_HEADING_LEVELS:
                for p in members:
                    r = p["role"]
                    r["role"], r["label"] = "unclassified", ROLE_LABELS["unclassified"]
                    r["unclassified_reason"] = "제목 후보 그룹이 %d개를 초과(%d번째 그룹) - 대·중·소 범위 밖" % (
                        MAX_HEADING_LEVELS, rank + 1)
                continue
            level = rank + 1
            prev_key = ordered[rank - 1] if rank > 0 else None
            # 인접 수준과 글자 크기로 구분되면 확실, 번호/굵기로만 구분되면 감쇠
            by_size = prev_key is None or prev_key[1] != key[1]
            factor = 0.95 if by_size else 0.85
            for p in members:
                r = p["role"]
                r["role"] = "heading_%d" % level
                r["label"] = ROLE_LABELS[r["role"]]
                r["confidence"] = round(r["confidence"] * factor, 3)
                r["evidence"].insert(0, {
                    "role": r["role"], "rule": "heading_level_rank", "weight": 0,
                    "detail": "제목 후보 %d개 그룹 중 %d번째 (기준: %s)%s" % (
                        len(ordered), level, self._key_desc(key),
                        "" if by_size else " - 상위 그룹과 글자 크기가 같아 번호/굵기로 구분, 신뢰도 감쇠"),
                })
                if r["confidence"] < ACCEPT_THRESHOLD:
                    r["role"], r["label"] = "unclassified", ROLE_LABELS["unclassified"]
                    r["unclassified_reason"] = "수준 부여 후 신뢰도 %.2f < 기준" % r["confidence"]

    def _heading_key(self, p):
        an = p.get("auto_numbering") or {}
        outline = an.get("level") if an.get("type") == "OUTLINE" else 99
        size = self._size(p) or 0
        marker = p.get("list_marker") or {}
        rank = marker.get("rank") if marker.get("rank") is not None else 50
        bold = 0 if (p.get("char_format") or {}).get("bold") else 1
        return (outline, -size, rank, bold)

    @staticmethod
    def _key_desc(key):
        outline, neg_size, rank, bold = key
        parts = []
        if outline != 99:
            parts.append("개요 %d수준" % outline)
        parts.append("%.1fpt" % -neg_size)
        if rank != 50:
            kind = [k for k, _, r in NUM_PATTERNS if r == rank]
            parts.append("번호 순위 %d(%s)" % (rank, "/".join(kind)))
        parts.append("진하게" if bold == 0 else "보통")
        return ", ".join(parts)


def marker_is_bullet(marker):
    return bool(marker) and marker["type"] == "bullet"
