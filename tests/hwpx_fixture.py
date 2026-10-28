"""테스트용 합성 HWPX 생성기.

실제 한글 저장 파일(샘플 5종)에서 관측한 구조를 최소 형태로 재현한다.
값은 테스트에서 기대값으로 사용하므로 바꾸면 테스트도 함께 바꿔야 한다.

구성(문서 순서):
  p0  secPr + 머리말 ctrl + "보고서 제목" 20pt HY헤드라인M 가운데  -> 문서 제목
  p1  "Ⅰ. 추진 배경" 15pt HY헤드라인M                               -> 대제목
  p2  "1. 현황 분석" 12pt 휴먼명조 진하게                              -> 중제목
  p3  본문 문장 12pt 휴먼명조 양쪽 160%, 각주 포함                     -> 본문
  p4  본문 문장 (탭/줄바꿈 혼합 내용, 글자 모양 2종 혼재)               -> 본문
  p5  "※ 자료: 내부 통계" 10pt                                          -> 참고·주석
  p6  빈 문단                                                           -> 빈 문단
  p7  "<표 1> 연도별 실적" 가운데                                       -> 표 제목
  p8  표(3행 2열, 첫 행 병합, 셀 가운데 정렬 110%)                      -> 개체 배치 문단
  p9  "<그림 1> 처리 흐름도" 가운데                                     -> 그림 캡션
  p10 그림                                                              -> 개체 배치 문단
  p11 "기타" 12pt 짧은 일반 문단                                        -> 미분류(근거 부족)
"""

import zipfile

NS_DECL = (
    'xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app" '
    'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
    'xmlns:hp10="http://www.hancom.co.kr/hwpml/2016/paragraph" '
    'xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
    'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" '
    'xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head" '
    'xmlns:hpf="http://www.hancom.co.kr/schema/2011/hpf" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:opf="http://www.idpf.org/2007/opf/" '
    'xmlns:hwpunitchar="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar"'
)
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
LANGS = ("HANGUL", "LATIN", "HANJA", "JAPANESE", "OTHER", "SYMBOL", "USER")

FONTS = ["휴먼명조", "HY헤드라인M", "맑은 고딕"]


def _fontfaces():
    out = []
    for lang in LANGS:
        fonts = "".join('<hh:font id="%d" face="%s" type="TTF" isEmbedded="0"/>' % (i, f) for i, f in enumerate(FONTS))
        out.append('<hh:fontface lang="%s" fontCnt="%d">%s</hh:fontface>' % (lang, len(FONTS), fonts))
    return '<hh:fontfaces itemCnt="7">%s</hh:fontfaces>' % "".join(out)


def _lang_attrs(v):
    return " ".join('%s="%s"' % (k, v) for k in ("hangul", "latin", "hanja", "japanese", "other", "symbol", "user"))


def _char_pr(cid, height, font, ratio=100, spacing=0, bold=False):
    return (
        '<hh:charPr id="%d" height="%d" textColor="#000000" shadeColor="none" useFontSpace="0" useKerning="0" '
        'symMark="NONE" borderFillIDRef="1"><hh:fontRef %s/><hh:ratio %s/><hh:spacing %s/><hh:relSz %s/>'
        '<hh:offset %s/>%s<hh:underline type="NONE" shape="SOLID" color="#000000"/>'
        '<hh:strikeout shape="NONE" color="#000000"/><hh:outline type="NONE"/>'
        '<hh:shadow type="NONE" color="#C0C0C0" offsetX="10" offsetY="10"/></hh:charPr>'
    ) % (cid, height, _lang_attrs(font), _lang_attrs(ratio), _lang_attrs(spacing), _lang_attrs(100),
         _lang_attrs(0), "<hh:bold/>" if bold else "")


def _margin(indent, left, right, prev, next_):
    return ('<hh:margin><hc:intent value="%d" unit="HWPUNIT"/><hc:left value="%d" unit="HWPUNIT"/>'
            '<hc:right value="%d" unit="HWPUNIT"/><hc:prev value="%d" unit="HWPUNIT"/>'
            '<hc:next value="%d" unit="HWPUNIT"/></hh:margin>') % (indent, left, right, prev, next_)


def _para_pr(pid, align, line, prev=0, next_=0, left=0, heading="NONE", level=0, hangul_break="KEEP_WORD"):
    # 한글 저장 파일과 같이 hp:switch 의 case(HwpUnitChar) 값과 default 값(2배)을 함께 기록.
    # hangul_break: 줄 나눔 기준 한글 단위. 한글 저장값은 이름과 반대(KEEP_WORD=글자, BREAK_WORD=어절)
    ls = '<hh:lineSpacing type="PERCENT" value="%d" unit="HWPUNIT"/>' % line
    return (
        '<hh:paraPr id="%d" tabPrIDRef="0" condense="0" fontLineHeight="0" snapToGrid="1" suppressLineNumbers="0" '
        'checked="0"><hh:align horizontal="%s" vertical="BASELINE"/><hh:heading type="%s" idRef="0" level="%d"/>'
        '<hh:breakSetting breakLatinWord="KEEP_WORD" breakNonLatinWord="%s" widowOrphan="0" '
        'keepWithNext="0" keepLines="0" pageBreakBefore="0" lineWrap="BREAK"/>'
        '<hh:autoSpacing eAsianEng="0" eAsianNum="0"/>'
        '<hp:switch><hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar">%s%s</hp:case>'
        '<hp:default>%s%s</hp:default></hp:switch>'
        '<hh:border borderFillIDRef="1" offsetLeft="0" offsetRight="0" offsetTop="0" offsetBottom="0" '
        'connect="0" ignoreMargin="0"/></hh:paraPr>'
    ) % (pid, align, heading, level, hangul_break, _margin(0, left, 0, prev, next_), ls,
         _margin(0, left * 2, 0, prev * 2, next_ * 2), ls)


# charPr: 0 제목 20pt HY헤드라인M / 1 대제목 15pt HY헤드라인M / 2 본문 12pt 휴먼명조
#         3 중제목 12pt 휴먼명조 진하게 / 4 주석 10pt 휴먼명조 / 5 표 11pt 맑은 고딕 장평95 자간-5
CHAR_PRS = [
    _char_pr(0, 2000, 1, spacing=-2),
    _char_pr(1, 1500, 1),
    _char_pr(2, 1200, 0),
    _char_pr(3, 1200, 0, bold=True),
    _char_pr(4, 1000, 0),
    _char_pr(5, 1100, 2, ratio=95, spacing=-5),
]
# paraPr: 0 본문 양쪽 160% / 1 가운데 160% / 2 대제목 문단위 10pt 아래 5pt / 3 주석 130% 왼쪽 10pt
#         4 표 셀 가운데 110% (줄 나눔 한글 단위 '어절' = BREAK_WORD, 나머지는 '글자' = KEEP_WORD)
PARA_PRS = [
    _para_pr(0, "JUSTIFY", 160),
    _para_pr(1, "CENTER", 160),
    _para_pr(2, "JUSTIFY", 160, prev=1000, next_=500),
    _para_pr(3, "JUSTIFY", 130, left=1000),
    _para_pr(4, "CENTER", 110, hangul_break="BREAK_WORD"),
    # 5: 한글 2016 형식처럼 hh:heading 을 hp:switch(2016/paragraph) 로 감싼 사례. 본문에서는 사용하지 않음
    _para_pr(5, "JUSTIFY", 160).replace(
        '<hh:heading type="NONE" idRef="0" level="0"/>',
        '<hp:switch><hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/paragraph">'
        '<hh:heading type="OUTLINE" idRef="0" level="8"/></hp:case>'
        '<hp:default><hh:heading type="NONE" idRef="0" level="0"/></hp:default></hp:switch>'),
]

HEADER = (
    XML_HEAD + '<hh:head %s version="1.5" secCnt="1">'
    '<hh:beginNum page="1" footnote="1" endnote="1" pic="1" tbl="1" equation="1"/><hh:refList>%s'
    '<hh:borderFills itemCnt="2"><hh:borderFill id="1" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">'
    '<hh:leftBorder type="NONE" width="0.1 mm" color="#000000"/><hh:rightBorder type="NONE" width="0.1 mm" color="#000000"/>'
    '<hh:topBorder type="NONE" width="0.1 mm" color="#000000"/><hh:bottomBorder type="NONE" width="0.1 mm" color="#000000"/>'
    '</hh:borderFill><hh:borderFill id="2" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">'
    '<hh:leftBorder type="SOLID" width="0.12 mm" color="#000000"/><hh:rightBorder type="SOLID" width="0.12 mm" color="#000000"/>'
    '<hh:topBorder type="SOLID" width="0.12 mm" color="#000000"/><hh:bottomBorder type="SOLID" width="0.12 mm" color="#000000"/>'
    '<hc:fillBrush><hc:winBrush faceColor="#D6D6D6" hatchColor="#000000" alpha="0"/></hc:fillBrush></hh:borderFill></hh:borderFills>'
    '<hh:charProperties itemCnt="%d">%s</hh:charProperties>'
    '<hh:paraProperties itemCnt="%d">%s</hh:paraProperties>'
    '<hh:styles itemCnt="2"><hh:style id="0" type="PARA" name="바탕글" engName="Normal" paraPrIDRef="0" charPrIDRef="2" '
    'nextStyleIDRef="0" langID="1042" lockForm="0"/><hh:style id="1" type="PARA" name="머리말" engName="Header" '
    'paraPrIDRef="1" charPrIDRef="4" nextStyleIDRef="1" langID="1042" lockForm="0"/></hh:styles>'
    '</hh:refList></hh:head>'
) % (NS_DECL, _fontfaces(), len(CHAR_PRS), "".join(CHAR_PRS), len(PARA_PRS), "".join(PARA_PRS))


def _p(para, char, inner, style=0):
    return '<hp:p id="0" paraPrIDRef="%d" styleIDRef="%d" pageBreak="0" columnBreak="0" merged="0">%s</hp:p>' % (
        para, style, inner if inner.startswith("<hp:run") else '<hp:run charPrIDRef="%d">%s</hp:run>' % (char, inner))


def _t(s):
    return "<hp:t>%s</hp:t>" % s


SEC_PR = (
    '<hp:secPr id="" textDirection="HORIZONTAL" spaceColumns="1134" tabStop="8000" tabStopVal="4000" '
    'tabStopUnit="HWPUNIT" outlineShapeIDRef="1" memoShapeIDRef="0" textVerticalWidthHead="0" masterPageCnt="0">'
    '<hp:grid lineGrid="0" charGrid="0" wonggojiFormat="0"/>'
    '<hp:startNum pageStartsOn="BOTH" page="0" pic="0" tbl="0" equation="0"/>'
    '<hp:pagePr landscape="WIDELY" width="59528" height="84188" gutterType="LEFT_ONLY">'
    '<hp:margin header="2835" footer="2835" gutter="0" left="5669" right="5669" top="5669" bottom="2835"/></hp:pagePr>'
    '</hp:secPr><hp:ctrl><hp:colPr id="" type="NEWSPAPER" layout="LEFT" colCount="1" sameSz="1" sameGap="0"/></hp:ctrl>'
)
HEADER_CTRL = (
    '<hp:ctrl><hp:header id="1" applyPageType="BOTH"><hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" '
    'vertAlign="TOP" linkListIDRef="0" linkListNextIDRef="0" textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0">'
    + _p(1, 4, _t("머리말 문구"), style=1) + '</hp:subList></hp:header></hp:ctrl>'
)
FOOTNOTE = (
    '<hp:ctrl><hp:footNote number="1" suffixChar="41" instId="1"><hp:subList id="" textDirection="HORIZONTAL" '
    'lineWrap="BREAK" vertAlign="TOP" linkListIDRef="0" linkListNextIDRef="0" textWidth="0" textHeight="0" '
    'hasTextRef="0" hasNumRef="0">' + _p(3, 4, _t("각주 내용입니다.")) + '</hp:subList></hp:footNote></hp:ctrl>'
)


def _cell(row, col, text, colspan=1, header=False):
    return (
        '<hp:tc name="" header="%d" hasMargin="0" protect="0" editable="0" dirty="0" borderFillIDRef="2">'
        '<hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" vertAlign="CENTER" linkListIDRef="0" '
        'linkListNextIDRef="0" textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0">%s</hp:subList>'
        '<hp:cellAddr colAddr="%d" rowAddr="%d"/><hp:cellSpan colSpan="%d" rowSpan="1"/>'
        '<hp:cellSz width="%d" height="1000"/><hp:cellMargin left="510" right="510" top="141" bottom="141"/></hp:tc>'
    ) % (1 if header else 0, _p(4, 5, _t(text)), col, row, colspan, 20000 * colspan)


TABLE = (
    '<hp:tbl id="100" zOrder="0" numberingType="TABLE" textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" lock="0" '
    'dropcapstyle="None" pageBreak="CELL" repeatHeader="1" rowCnt="3" colCnt="2" cellSpacing="0" borderFillIDRef="2" '
    'noAdjust="0"><hp:sz width="40000" widthRelTo="ABSOLUTE" height="3000" heightRelTo="ABSOLUTE" protect="0"/>'
    '<hp:pos treatAsChar="1" affectLSpacing="0" flowWithText="1" allowOverlap="0" holdAnchorAndSO="0" vertRelTo="PARA" '
    'horzRelTo="PARA" vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>'
    '<hp:outMargin left="283" right="283" top="283" bottom="283"/><hp:inMargin left="510" right="510" top="141" bottom="141"/>'
    '<hp:tr>' + _cell(0, 0, "연도별 실적", colspan=2, header=True) + '</hp:tr>'
    '<hp:tr>' + _cell(1, 0, "2024") + _cell(1, 1, "1,234") + '</hp:tr>'
    '<hp:tr>' + _cell(2, 0, "2025") + _cell(2, 1, "5,678") + '</hp:tr></hp:tbl>'
)
PICTURE = (
    '<hp:pic id="200" zOrder="1" numberingType="PICTURE" textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" lock="0" '
    'dropcapstyle="None" href="" groupLevel="0" instid="1" reverse="0"><hp:offset x="0" y="0"/>'
    '<hp:orgSz width="28346" height="14173"/><hp:curSz width="28346" height="14173"/>'
    '<hp:flip horizontal="0" vertical="0"/><hp:rotationInfo angle="0" centerX="0" centerY="0" rotateimage="1"/>'
    '<hc:img binaryItemIDRef="image1" bright="0" contrast="0" effect="REAL_PIC" alpha="0"/>'
    '<hp:sz width="28346" widthRelTo="ABSOLUTE" height="14173" heightRelTo="ABSOLUTE" protect="0"/>'
    '<hp:pos treatAsChar="1" affectLSpacing="0" flowWithText="1" allowOverlap="0" holdAnchorAndSO="0" vertRelTo="PARA" '
    'horzRelTo="PARA" vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>'
    '<hp:outMargin left="0" right="0" top="0" bottom="0"/></hp:pic>'
)

# 한글 저장 파일의 hp:linesegarray 형태(2줄). 줄 배치 캐시 추출 테스트용
LINESEG_2 = ('<hp:linesegarray><hp:lineseg textpos="0" vertpos="0" vertsize="1200" textheight="1200" baseline="1020" '
             'spacing="720" horzpos="0" horzsize="48188" flags="393216"/><hp:lineseg textpos="30" vertpos="1920" '
             'vertsize="1200" textheight="1200" baseline="1020" spacing="720" horzpos="0" horzsize="48188" flags="393216"/>'
             '</hp:linesegarray>')
BODY1 = "현재 사업은 계획대로 추진되고 있으며 주요 지표가 전년 대비 개선되었다."
BODY2_A = "세부 항목은"
BODY2_B = "다음과 같이 정리하였다."

SECTION = XML_HEAD + '<hs:sec %s>%s</hs:sec>' % (NS_DECL, "".join([
    _p(1, 0, '<hp:run charPrIDRef="0">%s%s</hp:run><hp:run charPrIDRef="0">%s</hp:run>' % (SEC_PR, HEADER_CTRL, _t("보고서 제목"))),
    _p(2, 1, _t("Ⅰ. 추진 배경")),
    _p(0, 3, _t("1. 현황 분석")),
    _p(0, 2, '<hp:run charPrIDRef="2">%s</hp:run><hp:run charPrIDRef="2">%s</hp:run>%s' % (_t(BODY1), FOOTNOTE, LINESEG_2)),
    _p(0, 2, '<hp:run charPrIDRef="2"><hp:t>%s<hp:tab width="4000" leader="0" type="1"/>%s</hp:t></hp:run>'
             '<hp:run charPrIDRef="3"><hp:t><hp:lineBreak/>강조</hp:t></hp:run><hp:run charPrIDRef="2"><hp:t> 문구를 포함한 문장이다.</hp:t></hp:run>' % (BODY2_A, BODY2_B)),
    _p(3, 4, _t("※ 자료: 내부 통계")),
    _p(0, 2, ""),
    _p(1, 2, _t("&lt;표 1&gt; 연도별 실적")),
    _p(1, 2, '<hp:run charPrIDRef="2">%s</hp:run>' % TABLE),
    _p(1, 2, _t("&lt;그림 1&gt; 처리 흐름도")),
    _p(1, 2, '<hp:run charPrIDRef="2">%s</hp:run>' % PICTURE),
    _p(0, 2, _t("기타")),
]))

CONTENT_HPF = (
    XML_HEAD + '<opf:package %s version="" unique-identifier="" id=""><opf:metadata>'
    '<opf:title>테스트 문서</opf:title><opf:language>ko</opf:language></opf:metadata><opf:manifest>'
    '<opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>'
    '<opf:item id="image1" href="BinData/image1.png" media-type="image/png" isEmbeded="1"/>'
    '<opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>'
    '<opf:item id="settings" href="settings.xml" media-type="application/xml"/></opf:manifest>'
    '<opf:spine><opf:itemref idref="header" linear="yes"/><opf:itemref idref="section0" linear="yes"/></opf:spine>'
    '</opf:package>'
) % NS_DECL
CONTAINER = (
    XML_HEAD + '<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container" '
    'xmlns:hpf="http://www.hancom.co.kr/schema/2011/hpf"><ocf:rootfiles>'
    '<ocf:rootfile full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/>'
    '</ocf:rootfiles></ocf:container>'
)
VERSION = (XML_HEAD + '<hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version" tagetApplication="WORDPROCESSOR" '
           'major="5" minor="1" micro="1" buildNumber="0" os="1" xmlVersion="1.5" application="Synthetic Fixture" appVersion="0"/>')
# 1x1 투명 PNG
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a5f1e3f80000000049454e44ae426082")


def build_fixture(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/hwp+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("version.xml", VERSION, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("Contents/header.xml", HEADER, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("Contents/section0.xml", SECTION, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("BinData/image1.png", PNG_1PX, compress_type=zipfile.ZIP_STORED)
        z.writestr("settings.xml", XML_HEAD + '<ha:HWPApplicationSetting xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app"/>',
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("META-INF/container.xml", CONTAINER, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("Contents/content.hpf", CONTENT_HPF, compress_type=zipfile.ZIP_DEFLATED)
    return path
