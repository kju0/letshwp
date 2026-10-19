"""XML 공통 유틸리티.

HWPX 는 동일한 지역 이름이 여러 네임스페이스 버전(2011/2016)으로 나타날 수 있으므로
태그 비교는 지역 이름(local name) 기준으로 수행한다.
"""

import xml.etree.ElementTree as ET

# 관측된 한컴 OWPML 네임스페이스 (Contents/header.xml 루트 선언 기준)
NS = {
    "hh": "http://www.hancom.co.kr/hwpml/2011/head",
    "hp": "http://www.hancom.co.kr/hwpml/2011/paragraph",
    "hp10": "http://www.hancom.co.kr/hwpml/2016/paragraph",
    "hs": "http://www.hancom.co.kr/hwpml/2011/section",
    "hc": "http://www.hancom.co.kr/hwpml/2011/core",
    "opf": "http://www.idpf.org/2007/opf/",
    "ocf": "urn:oasis:names:tc:opendocument:xmlns:container",
}

# hp:switch/hp:case 에서 이 분석기가 의미를 이해하는 required-namespace.
# 한글(Hancom Office)은 이 확장을 지원하므로 문서를 열 때 case 분기를 사용한다.
# 관측 사례(샘플 5종):
#   2016/HwpUnitChar : paraPr 의 hh:margin + hh:lineSpacing (default 의 margin 값은 case 의 정확히 2배)
#   2016/paragraph   : paraPr 의 hh:heading (case=OUTLINE 8~10수준, default=NONE) — 2016 형식 확장
#   2016/HwpUnitChar : tabPr 의 hh:tabItem
UNDERSTOOD_CASE_NAMESPACES = {
    "http://www.hancom.co.kr/hwpml/2016/HwpUnitChar",
    "http://www.hancom.co.kr/hwpml/2016/paragraph",
}

REQUIRED_NS_ATTR = "{%s}required-namespace" % NS["hp"]


class UnsafeXMLError(ValueError):
    pass


def parse_xml(data: bytes):
    """DTD/엔티티 선언이 있는 XML 은 거부한다(엔티티 확장 공격 방지).
    HWPX 표준 파트에는 DOCTYPE 이 사용되지 않는다."""
    head = data[:4096]
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data:
        raise UnsafeXMLError("DOCTYPE/ENTITY 선언이 포함된 XML 은 분석하지 않습니다.")
    return ET.fromstring(data)


def ln(tag):
    """'{ns}local' -> 'local'"""
    if tag is None or not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def children(elem, name):
    return [c for c in elem if ln(c.tag) == name]


def child(elem, name):
    for c in elem:
        if ln(c.tag) == name:
            return c
    return None


def iter_local(elem, name):
    for e in elem.iter():
        if ln(e.tag) == name:
            yield e


def to_int(v, default=None):
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def to_bool(v):
    if v is None:
        return None
    return str(v).strip().lower() in ("1", "true")


def effective_children(parent):
    """hp:switch 를 해석한 '실제 적용되는' 자식 목록.

    hp:switch 는 MCE AlternateContent 와 같은 규칙을 따른다고 보고,
    required-namespace 를 이해하는 첫 hp:case 를, 없으면 hp:default 를 선택한다.
    반환: [(element, source, default 분기 element 또는 None)]
    """
    out = []
    for c in parent:
        if ln(c.tag) != "switch":
            out.append((c, "direct", None))
            continue
        chosen, source = None, None
        for b in c:
            if ln(b.tag) == "case" and b.get(REQUIRED_NS_ATTR) in UNDERSTOOD_CASE_NAMESPACES:
                chosen, source = b, "switch:case(%s)" % b.get(REQUIRED_NS_ATTR).replace("http://www.hancom.co.kr/hwpml/", "")
                break
        default = child(c, "default")
        if chosen is None and default is not None:
            chosen, source = default, "switch:default"
        if chosen is None:
            continue
        for g in chosen:
            alt = child(default, ln(g.tag)) if (default is not None and chosen is not default) else None
            out.append((g, source, alt))
    return out


def resolve_switch_child(parent, name):
    """effective_children 중 지역 이름이 name 인 첫 요소.
    반환: (element | None, source 문자열 | None, default 분기의 같은 요소 | None)"""
    for e, source, alt in effective_children(parent):
        if ln(e.tag) == name:
            return e, source, alt
    return None, None, None
