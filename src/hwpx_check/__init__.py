"""HWPX 표준 서식 검사 패키지.

Inspector(src/inspector.py) 의 분석 결과 JSON 을 입력으로 받아
config/format_standard.json 의 표준값과 비교한다. 원본 HWPX 는 수정하지 않는다.

- rules  : 검사 규칙(규칙별 독립 함수 + 레지스트리)
- report : 사람이 읽는 TXT 보고서
"""

CHECKER_VERSION = "0.1.0"
