---
description: 개발 모드 — 기능 추가·버그 수정·개선 (git 반영 없음)
---
CLAUDE.md 의 "작업 모드 > 1) 개발 모드" 절차를 따른다.

요청: $ARGUMENTS

1. docs/PROJECT_CONTEXT.md 와 FORMAT_SPEC.md 에서 관련 맥락을 확인한다. 모호한 해석은 사용자에게 묻는다.
2. 코드·설정·테스트를 수정한다(절대 원칙 준수, 불필요한 리팩터링 금지).
3. `python3 -m unittest discover -s tests` 를 실행한다. 실패하면 고친다.
4. 관련 샘플이 있으면 `python3 scripts/run_test.py` 로 결과 변화를 확인한다.
5. docs/PROJECT_CONTEXT.md 를 갱신한다.
6. 결과 중심의 한국어로 보고한다. commit/push 는 하지 않는다.
