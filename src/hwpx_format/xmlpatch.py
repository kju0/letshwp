"""원본 XML 바이트를 보존한 채 필요한 위치만 고치는 도구.

ElementTree 로 다시 쓰면 네임스페이스 접두어·속성 따옴표·빈 요소 표기·선언문이 바뀔 수 있다.
한글이 저장한 형태를 그대로 두기 위해 expat 으로 요소의 바이트 위치만 기록하고,
속성값 교체·요소 복제 삽입을 바이트 단위로 적용한다(나머지 바이트는 한 글자도 바꾸지 않는다).
"""

import re
import xml.parsers.expat


class XmlPatchError(RuntimeError):
    pass


class Elem:
    __slots__ = ("qname", "local", "attrs", "start", "stag_end", "end", "parent", "children", "ordinal")

    def __init__(self, qname, attrs, start, parent, ordinal):
        self.qname = qname
        self.local = qname.split(":", 1)[-1]
        self.attrs = attrs
        self.start = start          # '<' 위치
        self.stag_end = None        # 시작 태그 '>' 다음 위치
        self.end = None             # 요소 전체의 끝(종료 태그 '>' 다음) 위치
        self.parent = parent
        self.children = []
        self.ordinal = ordinal      # 문서 순서 번호(같은 local 이름 기준 아님, 전체 요소 기준)

    def iter(self, local=None):
        stack = [self]
        while stack:
            e = stack.pop()
            if local is None or e.local == local:
                yield e
            stack.extend(reversed(e.children))

    def kids(self, local):
        return [c for c in self.children if c.local == local]

    def has_ancestor(self, local, stop=None):
        e = self.parent
        while e is not None and e is not stop:
            if e.local == local:
                return True
            e = e.parent
        return False


def _tag_end(data, start):
    """data[start] == '<' 인 시작/종료 태그의 '>' 다음 위치(따옴표 안의 '>' 는 건너뜀)."""
    i, quote = start + 1, None
    n = len(data)
    while i < n:
        ch = data[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in (0x22, 0x27):  # " '
            quote = ch
        elif ch == 0x3E:  # >
            return i + 1
        i += 1
    raise XmlPatchError("태그 끝을 찾지 못함(위치 %d)" % start)


def scan(data: bytes):
    """XML 바이트를 읽어 요소 트리(바이트 위치 포함)를 만든다. 반환: (root Elem, 문서 순서 요소 목록)"""
    parser = xml.parsers.expat.ParserCreate()  # 네임스페이스 처리 없이 qname 그대로
    stack, order = [], []
    root = []

    def start(name, attrs):
        pos = parser.CurrentByteIndex
        e = Elem(name, attrs, pos, stack[-1] if stack else None, len(order))
        e.stag_end = _tag_end(data, pos)
        if stack:
            stack[-1].children.append(e)
        else:
            root.append(e)
        stack.append(e)
        order.append(e)

    def end(name):
        e = stack.pop()
        if data[e.stag_end - 2:e.stag_end] == b"/>":
            e.end = e.stag_end
        else:
            e.end = _tag_end(data, parser.CurrentByteIndex)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(data, True)
    except xml.parsers.expat.ExpatError as ex:
        raise XmlPatchError("XML 해석 실패: %s" % ex)
    if not root:
        raise XmlPatchError("루트 요소가 없음")
    return root[0], order


def _attr_regex(name):
    return re.compile(rb"(\s" + re.escape(name.encode("utf-8")) + rb"\s*=\s*)([\"'])(.*?)\2", re.S)


def set_attr_edit(data, elem, name, value):
    """elem 시작 태그 안의 속성 name 값을 value 로 바꾸는 편집 (start, end, bytes) 를 만든다.
    속성이 없으면 새로 추가하지 않고 오류(구조를 추측해 바꾸지 않기 위해)."""
    tag = data[elem.start:elem.stag_end]
    m = _attr_regex(name).search(tag)
    if not m:
        raise XmlPatchError("<%s> 에 속성 %s 가 없음" % (elem.qname, name))
    s = elem.start + m.start(3)
    return (s, s + len(m.group(3)), _escape(str(value)))


def _escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;").encode("utf-8")


def apply_edits(data: bytes, edits):
    """겹치지 않는 (start, end, new_bytes) 편집을 적용한다."""
    edits = sorted(edits, key=lambda e: e[0])
    for a, b in zip(edits, edits[1:]):
        if a[1] > b[0]:
            raise XmlPatchError("겹치는 XML 편집(%d-%d, %d-%d)" % (a[0], a[1], b[0], b[1]))
    out, pos = [], 0
    for s, e, new in edits:
        out.append(data[pos:s])
        out.append(new)
        pos = e
    out.append(data[pos:])
    return b"".join(out)
