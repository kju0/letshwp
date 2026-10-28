---
description: 배포 모드 — 커밋·push 된 코드를 Tailscale 서버에 배포하고 Mattermost 봇을 재시작(상태 확인·롤백 포함)
---
CLAUDE.md 의 "작업 모드 > 5) 배포 모드" 절차를 따른다.

요청: $ARGUMENTS

1. 요청을 구분한다: 배포 / 상태 확인 / 롤백 / 로그.
2. 배포:
   - `git status` 에 커밋 안 된 변경이 있거나 HEAD 가 origin/main 에 없으면 멈추고, push 모드를 먼저 진행할지 묻는다.
   - `python3 scripts/deploy.py deploy` 를 실행한다.
3. 상태: `python3 scripts/deploy.py status`. 롤백: `python3 scripts/deploy.py rollback`. 로그: `python3 scripts/deploy.py logs -n 50`.
4. 종료 코드를 확인한다(0 성공, 2 로컬 검사·테스트 실패, 3 서버 테스트 실패, 4 서버 반영 실패·자동 복구, 5 접속 실패).
5. 실행 출력에 있는 릴리스 이름·커밋·테스트 결과·봇 연결 여부만 그대로 보고한다. 실패면 정상 배포로 전달하지 않는다.
