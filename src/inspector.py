"""HWPX 서식 분석 엔진 (읽기 전용).

사용법
    python src/inspector.py <input.hwpx> [-o output.json] [--report report.txt]

라이브러리로 사용
    from inspector import inspect_hwpx, save_json
    result = inspect_hwpx("sample.hwpx")
    save_json(result, "output/sample.inspect.json")

원본 파일은 읽기만 하며, 분석 전후 SHA-256 을 비교해 불변을 검증한다.
결과 JSON 의 paragraphs[*].path 는 XML 상 위치를 결정적으로 나타내므로
(예: sec0/p12/tbl0/r1c2/p0) 추후 서식 비교·변경 모듈이 같은 요소를 찾아갈 수 있다.
"""

import argparse
import datetime
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hwpx_analysis import ANALYZER_VERSION  # noqa: E402
from hwpx_analysis.classifier import RoleClassifier, ROLE_LABELS  # noqa: E402
from hwpx_analysis.header import HeaderInfo  # noqa: E402
from hwpx_analysis.package import HwpxPackage, sha256_of  # noqa: E402
from hwpx_analysis.section import (  # noqa: E402
    SectionWalker, new_state, count_xml_elements, raw_text_of,
)
from hwpx_analysis.stats import aggregate, table_text_summary  # noqa: E402

SCHEMA_VERSION = "1.0"

# 추출 항목별 신뢰 수준. 보고서와 JSON 에 함께 기록한다.
#   exact     : XML 속성값을 규격 의미대로 직접 해석(단위 변환만 수행)
#   derived   : XML 값에서 결정적으로 계산(대표값 선택, 집계, 격자 검증 등)
#   heuristic : 규칙 기반 추정(결과에 근거·신뢰도 포함)
#   unverified: 값은 그대로 보고하지만 의미 해석이 공식 근거로 확인되지 않음
#   unsupported: 존재만 기록하거나 분석하지 않음
SUPPORT_MATRIX = [
    ("패키지 구조(mimetype, container, content.hpf manifest/spine)", "exact", ""),
    ("용지 크기(pagePr width/height) 및 여백(pagePr/margin)", "exact", "HWPUNIT→mm"),
    ("용지 이름(A4 등)", "derived", "치수 ±1mm 일치 시에만 부여"),
    ("용지 방향", "derived", "width/height 비교로 판단. landscape 속성(WIDELY/NARROWLY)의 의미는 미확인이라 원값만 보고"),
    ("글꼴(charPr/fontRef → fontface)", "exact", "7개 언어 슬롯별"),
    ("글자 크기(charPr@height)", "exact", "relSz 반영한 실효 크기도 함께 제공"),
    ("장평(ratio)·자간(spacing)·상대크기·글자위치", "exact", "언어별"),
    ("진하게/기울임/밑줄/취소선/색", "exact", ""),
    ("문단 정렬(align@horizontal)", "exact", ""),
    ("줄 간격(lineSpacing type/value)", "exact", "PERCENT 는 %, 그 외는 HWPUNIT→pt"),
    ("문단 여백·간격(margin: indent/left/right/prev/next)", "exact",
     "hp:switch 의 HwpUnitChar case 분기를 채택(한글이 사용하는 값). default 분기 원값 병기"),
    ("문단의 대표 글자 모양", "derived", "공백 제외 글자 수가 가장 많은 run 의 charPr. 혼합 여부 별도 표시"),
    ("개요/번호 문단의 자동 번호 형식", "derived", "OUTLINE 은 secPr@outlineShapeIDRef 의 numbering 사용"),
    ("본문 텍스트 속 항목 번호·기호(1. 가. □ ○ 등)", "derived", "텍스트 패턴 매칭"),
    ("표 구조(행·열·병합·셀 크기·셀 여백·테두리/배경)", "exact", ""),
    ("표 격자 무결성(병합 셀이 격자를 정확히 덮는지)", "derived", ""),
    ("그림(크기·바이너리 경로·배치)", "exact", ""),
    ("도형/글상자 텍스트", "exact", "글상자 내부 문단은 문맥 textbox 로 수집"),
    ("머리말·꼬리말·각주·미주 문단", "exact", ""),
    ("역할 분류(제목/본문/캡션/주석 등)", "heuristic", "근거·신뢰도 포함, 불확실 시 미분류"),
    ("수식·OLE·차트·양식 컨트롤 내부", "unsupported", "개체 존재와 크기만 기록"),
    ("실제 렌더링 결과(줄바꿈 위치, 쪽 번호, 어절 분리)", "unsupported",
     "hp:linesegarray 는 레이아웃 캐시로 신뢰하지 않음"),
    ("스타일 상속 해석", "unsupported", "OWPML 은 문단/글자에 완결된 모양을 직접 참조하므로 불필요. 스타일 이름만 보고"),
    ("마스터 페이지·바탕쪽", "unsupported", ""),
    ("변경 추적·메모 내용", "unsupported", ""),
]


class InspectionError(RuntimeError):
    pass


def inspect_hwpx(path):
    path = Path(path)
    if not path.is_file():
        raise InspectionError("파일이 없습니다: %s" % path)
    if path.suffix.lower() != ".hwpx":
        raise InspectionError("HWPX(.hwpx) 파일만 지원합니다: %s" % path)

    stat_before = path.stat()
    sha_before = sha256_of(path)

    warnings = []
    with HwpxPackage(path) as pkg:
        desc = pkg.describe()
        warnings.extend(pkg.warnings)
        if not desc["header_part"]:
            raise InspectionError("header.xml 을 찾을 수 없습니다.")
        header = HeaderInfo(pkg.read_xml(desc["header_part"]))
        warnings.extend(header.warnings)

        state = new_state()
        section_checks = []
        raw_chars = Counter()
        raw_unknown = Counter()
        for i, part in enumerate(desc["section_parts"]):
            root = pkg.read_xml(part)
            before = {k: v for k, v in state["xml_counts_visited"].items()}
            p_before = len(state["paragraphs"])
            SectionWalker(header, desc["binary_items"], i, state).walk(root)
            xml_counts = count_xml_elements(root)
            visited = {k: state["xml_counts_visited"][k] - before.get(k, 0) for k in ("p", "tbl", "pic")}
            raw = raw_text_of(root, raw_unknown)
            raw_chars.update(raw)
            section_checks.append({
                "part": part,
                "xml_counts": dict(xml_counts),
                "visited": visited,
                "paragraphs_recorded": len(state["paragraphs"]) - p_before,
                "all_paragraphs_visited": xml_counts.get("p", 0) == visited["p"],
                "all_tables_visited": xml_counts.get("tbl", 0) == visited["tbl"],
                "all_pictures_visited": xml_counts.get("pic", 0) == visited["pic"],
            })

    sha_after = sha256_of(path)
    stat_after = path.stat()
    unchanged = sha_before == sha_after and stat_before.st_mtime_ns == stat_after.st_mtime_ns
    if not unchanged:
        raise InspectionError("분석 중 원본 파일이 변경되었습니다. 결과를 신뢰할 수 없습니다.")

    paragraphs = state["paragraphs"]
    classification = RoleClassifier(paragraphs, state["tables"], state["pictures"], state["shapes"],
                                    desc["metadata"]).run()
    for t in state["tables"]:
        t["text_format_summary"] = table_text_summary(t, paragraphs)

    extracted_chars = Counter()
    for p in paragraphs:
        extracted_chars.update(p["text"])
    text_ok = extracted_chars == raw_chars

    verification = {
        "source_unchanged": unchanged,
        "sections": section_checks,
        "text_coverage": {
            "ok": text_ok,
            "raw_hp_t_chars": sum(raw_chars.values()),
            "extracted_chars": sum(extracted_chars.values()),
            "method": "섹션 XML 의 모든 hp:t 텍스트와 문단 추출 텍스트의 문자 다중집합 비교",
        },
        "tables_grid_ok": all(t["grid_check"]["ok"] for t in state["tables"]),
        "tables_grid_failures": [t["id"] for t in state["tables"] if not t["grid_check"]["ok"]],
        "unhandled_elements": dict(state["unknown"]),
        "all_ok": None,
    }
    verification["all_ok"] = bool(
        unchanged and text_ok and verification["tables_grid_ok"]
        and all(s["all_paragraphs_visited"] and s["all_tables_visited"] and s["all_pictures_visited"]
                for s in section_checks)
    )
    if state["unknown"]:
        warnings.append("처리 방법이 정의되지 않은 요소가 있습니다: %s" % dict(state["unknown"]))
    warnings.extend(state["warnings"])

    return {
        "schema_version": SCHEMA_VERSION,
        "analyzer": {
            "name": "letshwp-inspector",
            "version": ANALYZER_VERSION,
            "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        },
        "source": {
            "path": str(path.resolve()),
            "file_name": path.name,
            "size_bytes": stat_before.st_size,
            "sha256": sha_before,
            "opened_read_only": True,
            "unchanged_after_analysis": unchanged,
        },
        "package": {k: v for k, v in desc.items() if k != "binary_items"},
        "document": {
            "metadata": desc["metadata"],
            "sections": state["sections"],
            "fields": dict(state["fields"]),
        },
        "format_definitions": header.to_dict(),
        "paragraphs": paragraphs,
        "tables": state["tables"],
        "pictures": state["pictures"],
        "shapes": state["shapes"],
        "other_objects": state["other_objects"],
        "notes": state["notes"],
        "classification": dict(classification, role_labels=ROLE_LABELS),
        "statistics": aggregate(paragraphs, header),
        "verification": verification,
        "support_matrix": [{"item": a, "level": b, "note": c} for a, b, c in SUPPORT_MATRIX],
        "warnings": warnings,
    }


def _assert_safe_output(out_path, source_path=None):
    out = Path(out_path).resolve()
    if out.suffix.lower() == ".hwpx":
        raise InspectionError("결과 파일 확장자로 .hwpx 를 사용할 수 없습니다(원본 보호): %s" % out)
    if source_path is not None and out == Path(source_path).resolve():
        raise InspectionError("결과 경로가 원본 경로와 같습니다.")
    return out


def save_json(result, out_path):
    out = _assert_safe_output(out_path, result.get("source", {}).get("path"))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="HWPX 서식 분석 (읽기 전용)")
    ap.add_argument("input", help="분석할 .hwpx 파일")
    ap.add_argument("-o", "--output", help="결과 JSON 경로 (기본: output/<파일명>.inspect.json)")
    ap.add_argument("--report", help="사람이 읽는 텍스트 보고서 경로(선택)")
    args = ap.parse_args(argv)

    result = inspect_hwpx(args.input)
    out = args.output or os.path.join("output", Path(args.input).stem + ".inspect.json")
    saved = save_json(result, out)
    print("JSON 저장: %s" % saved)
    if args.report:
        from hwpx_analysis.report import render_report
        rp = _assert_safe_output(args.report, result["source"]["path"])
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(render_report(result), encoding="utf-8")
        print("보고서 저장: %s" % rp)
    v = result["verification"]
    print("검증: %s" % ("통과" if v["all_ok"] else "실패 - verification 항목 확인 필요"))
    return 0 if v["all_ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
