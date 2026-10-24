"""HWPX 서식 수정(Formatter) 패키지.

Checker 결과의 fix_plan 을 HWPX 복제본에 적용하고, 수정본을 다시 분석·검사해 검증한다.
원본 HWPX 는 수정하지 않는다.

- xmlpatch  : 원본 XML 바이트를 보존한 채 속성만 바꾸는 도구
- ops       : 수정 동작(op)별 독립 함수 + 레지스트리
- apply     : 서식 정의 복제·문단 재참조·패키지 쓰기
- verify    : 내용·구조 보존과 재검사 결과 검증
- report_md : 마크다운 수정 보고서
"""

FORMATTER_VERSION = "0.1.0"
