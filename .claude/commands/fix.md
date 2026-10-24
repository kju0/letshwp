---
description: 수정 모드 — hwpx 를 저장소 코드(검사 → 서식 수정 → 재검사·검증)로 실행하고 수정본과 수정 보고서 전달
---
CLAUDE.md 의 "작업 모드 > 4) 수정 모드" 절차를 따른다.

대상/옵션: $ARGUMENTS

1. 대상 hwpx 를 찾는다(첨부 파일 또는 경로). 저장소 밖 파일은 `inbox/` 에 복사한다. 없으면 파일을 요청한다.
2. `python3 scripts/run_fix.py <inbox 또는 samples 의 파일> [--doc-type A|B]` 를 실행한다.
3. 종료 코드와 run_info.json 의 ok 를 확인한다. 실패면 수정본이 없으므로 오류만 보고한다.
4. 결과 폴더 경로, 수정본 경로, 코드 버전, change_report.md 내용을 그대로 전달한다.
5. 문서를 AI 가 직접 고치거나, 코드가 내지 않은 판정·수정 내역을 덧붙이지 않는다.
