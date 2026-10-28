# letshwp
렛츠한글 — HWPX 문서의 내용과 구조는 그대로 두고 서식만 표준에 맞추는 도구.

현재 구성: **Inspector(분석)** → **Checker(표준 검사)** → **Formatter(서식 수정·검증)**

표준 라이브러리만 사용한다(Python 3.10+). 외부 패키지·네트워크 없이 폐쇄망에서 실행된다.

```
config/format_standard.json   표준값 설정 (FORMAT_SPEC.md 를 검사기가 읽는 형태로 옮긴 것)
src/inspector.py              분석기 CLI        src/hwpx_analysis/  분석 모듈
src/checker.py                검사기 CLI        src/hwpx_check/     검사 규칙·보고서
src/formatter.py              수정기 CLI        src/hwpx_format/    수정 동작·검증·마크다운 보고서
scripts/run_test.py           테스트 모드 실행기(Inspector → Checker → 결과 폴더)
scripts/run_fix.py            수정 모드 실행기(검사 → 수정 → 재검사·검증 → 결과 폴더)
tests/                        자동 테스트 (python3 -m unittest discover -s tests -v)
```

---

## 0. Claude Code 에서 사용하기 (코드·명령어 없이 대화로 작업)

사용자는 Claude Code 데스크톱 앱 또는 웹에서 아래 다섯 가지를 요청한다. 규칙은 CLAUDE.md "작업 모드", 지금까지의 맥락은 [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md) 에 있다.

| 모드 | 이렇게 말하기 | 하는 일 |
|---|---|---|
| 개발 | "개발: ○○ 기능 추가해줘" 또는 `/dev ○○` | 코드·테스트 수정, 테스트 실행 (git 반영 없음) |
| 원격 저장소 연결 | "push 해줘" / "pull 해줘" 또는 `/push`, `/pull` | 테스트 → 커밋 → GitHub push / 최신 코드 가져오기 |
| 테스트 | hwpx 첨부(또는 경로) + "테스트해줘" 또는 `/test 파일` | 저장소 코드로 Inspector → Checker 실행, 출력 파일 전달 |
| 수정 | hwpx 첨부(또는 경로) + "수정해줘" 또는 `/fix 파일` | 검사 → 서식 수정 → 재검사·검증, 수정본 hwpx 와 마크다운 수정 보고서 전달 |
| 배포 | "배포해줘" / "봇 상태" / "롤백해줘" 또는 `/deploy` | push 된 코드를 서버에 배포하고 Mattermost 봇 재시작·연결 확인 (실패 시 자동 복구) |

테스트 결과는 `output/<파일명>_<시각>/` 에 저장된다: `check_report.txt`(검사 보고서), `check.json`, `inspection_report.txt`, `inspection.json`, `run_info.json`(원본 무변경 확인, 실행한 코드 커밋), `source.hwpx`(원본 복사본).
수정 결과는 `output/<파일명>_<시각>_수정/` 에 저장된다: `<파일명>_수정본.hwpx`, `change_report.md`(수정 내역), `fix_result.json`, `check_before*`/`check_after*`(수정 전후 검사), `run_info.json`, `source.hwpx`.
결과는 항상 코드를 실행해서 얻으며, AI 가 문서를 직접 고치지 않는다.

- **데스크톱 앱**: Code 탭 → 폴더 `/Users/joo0/Desktop/dev/letshwp` 선택 → 대화 시작. 로컬 파일을 그대로 쓰므로 hwpx 첨부·경로 지정과 main push 가 모두 가능하다.
- **웹 (claude.ai/code)**: GitHub 저장소 `kju0/letshwp` 를 연결해 세션을 연다. 클라우드에 저장소를 복제해 실행하므로 **push 된 코드·규칙만** 보인다. 웹 세션은 보통 작업 브랜치를 만들어 push 하며(main 반영은 PR 병합), 결과 파일은 클라우드 환경에 생기므로 대화로 전달받는다. hwpx 첨부가 안 되면 데스크톱을 사용한다.

---

## 1. Inspector (분석기)

HWPX 의 문단·글자·표·그림·페이지 설정을 추출하고, 문단 역할(제목/본문/주석 등)을 근거·신뢰도와 함께 추정한다.

```
python3 src/inspector.py samples/문서.hwpx -o output/문서.json --report output/문서.txt
```

구조 분석 근거는 [docs/HWPX_STRUCTURE.md](docs/HWPX_STRUCTURE.md) 참조.

---

## 2. Checker (표준 서식 검사기)

### 역할
Inspector 결과를 [FORMAT_SPEC.md](FORMAT_SPEC.md) 의 표준(값은 `config/format_standard.json`)과 비교해서

- 어떤 항목이 표준에 맞는지(PASS), 다른지(FAIL)
- 판단에 필요한 정보가 부족한 항목은 무엇인지(UNKNOWN)
- 다를 경우 현재값과 표준값, 권장 수정 방법
- Formatter 가 바꿔야 할 서식 속성(XML 위치 포함)

을 JSON 과 TXT 로 출력한다. **원본 HWPX 는 수정하지 않는다.**

### 실행

```
# A. Inspector 결과 JSON 입력
python3 src/checker.py output/문서.json  -o output/문서_check.json --report output/문서_check.txt

# B. HWPX 직접 입력 (내부에서 Inspector 실행)
python3 src/checker.py samples/문서.hwpx -o output/문서_check.json --report output/문서_check.txt
```

- `-o` 생략 시 `output/<이름>_check.json` 에 저장. `--report` 는 선택.
- `--config 경로` 로 다른 표준값 설정 파일을 쓸 수 있다.
- `--doc-type A|B` 로 문서 유형을 지정하면 TYPE A/B 문단 간격 특례(5.6)를 검사한다.
  판별 기준이 미확정이라 자동 판별하지 않으며, 지정하지 않으면 특례 대상 문단은 판단 불가로 보고한다.
  - TYPE A: 첫 페이지 제목 표 → 첫 `1.` 항목 문단 위 20pt (제목 표가 문서 첫 요소이고 사이에 빈 문단만 있으며 쪽 나눔이 없을 때만)
  - TYPE B: 목차 다음 쪽 첫 `1.` 항목 문단 위 0pt (그 문단에 쪽 나눔 속성이 있을 때만)
- Inspector 검증(`verification.all_ok`)이 실패한 분석 결과는 검사하지 않고 종료한다(종료 코드 2).

### Inspector → Checker 연속 실행

```
python3 src/inspector.py samples/test1.hwpx -o output/test1-output.json --report output/test1-report.txt
python3 src/checker.py   output/test1-output.json -o output/test1_check.json --report output/test1_check.txt
```

분석 결과를 따로 볼 필요가 없으면 B 방식 한 번으로 같은 검사 결과를 얻는다(두 방식의 결과는 동일함을 테스트로 확인).

### 입력과 결과 파일

| 파일 | 내용 |
|---|---|
| 입력 `.json` | Inspector 결과 (`schema_version`, `paragraphs`, `format_definitions`, `verification` 필수) |
| 입력 `.hwpx` | 원본 문서. 읽기만 하며 검사 전후 SHA-256 을 비교한다 |
| `*_check.json` | 검사 결과 (아래 구조) |
| `*_check.txt` | 사람이 읽는 한국어 보고서 |

`*_check.json` 주요 키

| 키 | 내용 |
|---|---|
| `document` | 검사 문서명, 경로, SHA-256, 입력 방식, 원본 불변 여부, 문서 유형(`document_type`), 항목 계층 요약(`hierarchy`) |
| `summary` | 전체/상태별 건수, 항목별 건수 (`by_rule`) |
| `results[]` | 검사 결과 1건씩 (아래) |
| `fix_plan` | Formatter 입력: `preservation_rules`, `auto_actions`, `role_based_actions`, `manual_review` |

`results[]` 한 건의 필드

| 필드 | 의미 |
|---|---|
| `id`, `rule_id`, `rule_name`, `spec_mode` | 결과 ID, 검사 항목, FORMAT_SPEC 처리 방식 |
| `target` | 위치: `paragraph_index`(Inspector 문단 번호), `path`(XML 위치, 예 `sec0/p16/tbl0/r0c0/p0`), 표·셀 |
| `role` | Inspector 가 **추정한** 역할·신뢰도 (`source` 에 추정임을 표시) |
| `current` | HWPX 에서 **추출한 실제** 현재값과 참조 정의(`refs`: charPr/paraPr id) |
| `expected` | 표준값과 근거(FORMAT_SPEC 문구) |
| `status`, `reason` | PASS/FAIL/UNKNOWN/SKIP 과 판단 근거 |
| `fix` | `possible`(수정 가능 여부), `mode`, `recommendation`(권장 방법), `actions`(서식 변경 동작) |
| `unknown_reason`, `needed_info`, `observation` | 판단하지 못한 이유, 추가로 필요한 정보, 관찰 결과(빈 문단 중복 간격 등) |
| `sub_item` | 한 항목의 세부 검사(예: 내어쓰기의 `번호·기호 뒤 공백` / `둘째 줄 정렬`) |
| `hierarchy` | (문단 간 간격) 계층·번호, 비교 대상 항목(`compare_with`), 소속 항목(`owner_item`) |
| `interpretation_note` | 명세 6장 미확정 해석에 해당하는 판정에 붙는 주의 |

### 검사 결과 상태

| 상태 | 의미 |
|---|---|
| `PASS` | 실제 현재값과 표준값을 비교해 일치 |
| `FAIL` | 실제 현재값과 표준값을 비교해 다르며 수정 필요 |
| `UNKNOWN` | 판단 불가 — 현재값 없음, 역할 미분류/신뢰도 미달(기준 0.6), 표준값 미확정, '분석만' 항목의 불일치 |
| `SKIP` | 현재 검사 범위 밖 — 명세에 해당 역할 기준이 없음(머리말·차례·표 내부 글꼴 등), 렌더링 정보 필요 |

수정 구분(`fix.mode`): `auto`(자동 수정) / `auto_if_role_confirmed`(추정 역할 확인 후 자동 수정) /
`manual_review`(날짜·시간·번호·기호 등 텍스트 변경이 필요해 자동 수정하지 않음) / `none`(분석만).

### TXT 보고서 읽는 법

- **① 문서 검사 요약**: 상태별 건수와 항목별 표.
- **② 수정이 필요한 항목**: FAIL 1건마다 위치·구분(추정 역할)·현재값·표준값·판단 근거·수정 방법.
- **③ 표준에 적합한 항목**: 항목·표준값별 PASS 건수와 문단 번호.
- **④ 판단 불가 및 검사 제외 항목**: 같은 사유끼리 묶어 사유와 필요한 정보를 표시.
- **⑤ 향후 수정 계획**: 보존 조건과 Formatter 가 바꿀 charPr/paraPr/pagePr 목록.
  `정의 복제 필요`는 바꿀 정의를 다른 문단도 쓰고 있어 그대로 바꾸면 영향이 번진다는 뜻이다.
- 위치의 `#번호`는 **Inspector 문단 인덱스**(0부터, 표·머리말 문단 포함 문서 순서)이며 쪽 번호가 아니다.
  쪽 번호는 렌더링 없이는 알 수 없으므로 출력하지 않는다. `표 T1 1행 1열`은 1부터 센 셀 위치다.

### 지원 검사 항목

| 항목 | 처리 방식 | 판정 |
|---|---|---|
| 용지 크기 (A4) | 자동 수정 | PASS/FAIL |
| 용지 방향 (세로) | 자동 수정 | PASS 또는 UNKNOWN (landscape 속성 의미 미확인이라 FAIL 은 내지 않음) |
| 글꼴 (대·중제목 HY헤드라인M, 그 외 본문·주석·소제목·표 내부·머리말 등 휴먼명조) | 분석 후 수정 | PASS/FAIL, 문서 제목은 UNKNOWN(표지 여부 판별 불가) |
| 장평 100% / 자간 0% (한글·영문 슬롯) | 자동 수정 | PASS/FAIL (역할 무관) |
| 줄 간격 (본문 160, 주석 130, 표/그림 제목 110) | 분석 후 수정 | PASS/FAIL, 표 내부는 UNKNOWN |
| 문단 간 간격 (계층 16/15/14pt, 2↔3단계 14pt, 일반 본문 6pt, TYPE A 20pt / B 0pt) | 분석 후 수정 | PASS/FAIL, 계층 미정·소속 모호·유형 미지정은 UNKNOWN |
| 항목 줄바꿈 정렬 - 번호·기호 뒤 공백 한 칸 | 분석 후 수정 | PASS/FAIL (글자 변경 금지라 수동 검토) |
| 항목 줄바꿈 정렬 - 둘째 줄 정렬(내어쓰기) | 분석 후 수정 | 번호·기호 부분 폭을 글자 수로 근사해 내어쓰기와 비교(±1pt) → PASS/FAIL. 줄 수와 무관하게 모든 텍스트 번호·기호 문단(※ 등 표준에 없는 기호는 제외). 탭·폭 기준 없는 문자·자동 번호는 UNKNOWN |
| 문단 정렬 (본문·주석 양쪽, 표 내부 가운데) | 분석 후 수정 | PASS/FAIL, 제목류·숫자 셀·1×1 표는 UNKNOWN |
| 머리말·꼬리말 여백 10/10mm | 분석만 | 일치 시 PASS, 불일치 시 UNKNOWN |
| 문서 제목 박스 높이 15mm | 검사만 | PASS/FAIL (자동 수정 안 함) |
| 항목 번호 형식 (1. 가. 1) 가) ① ㉮) | 검사만 | PASS/FAIL (자동 수정 안 함) |
| 항목 기호 (□ ㅇ - ․, 영문 o 등 혼동 문자 감지) | 검사만 | PASS/FAIL (자동 수정 안 함) |
| 날짜 표기 (2015. 4. 2.) | 검사만 | PASS/FAIL (자동 수정 안 함) |
| 시간 표기 (24시간제 14:50) | 검사만 | PASS/FAIL, 'N시' 표기는 UNKNOWN |

### 미지원 / 판단 불가 항목

| 항목 | 이유 |
|---|---|
| 페이지 여백 20/20/20/10mm | 방향별 대응 미확정 → UNKNOWN (값 집합 일치 여부만 관찰) |
| 글자 크기 | 표준값 미확정 → UNKNOWN (역할별 분포 제공) |
| 기호(□ ㅇ - ․)·Ⅰ.·(1)·(가) 항목의 계층 | 명세 5.1 계층 표에 없음 → 그 항목과 다음 항목의 간격은 UNKNOWN. 확정되면 설정 `PARA_SPACING.bullet_levels` 에 지정(예 `{"□": 1}`) |
| TYPE A/B 문서 유형 | 판별 기준 미확정 → `--doc-type` 미지정 시 UNKNOWN |
| 내어쓰기 폭 | 실제 글꼴 너비가 아닌 글자 수 근사(한글·기호 1칸, 공백·숫자·영문·괄호 0.5칸, 1칸=글자 크기). 탭·`․`·`·` 등 폭 기준이 없는 문자가 있으면 UNKNOWN |
| 어절 단위 자간 조정 | 렌더링 결과 필요·방식 미확정 → SKIP |
| 제목 박스 양단선 1mm | '양단선' 정의 미확정 → UNKNOWN (위·아래 테두리로 해석 시 일치 여부만 관찰) |
| 제목 박스 필요 여부 | 표지·목차 판단 기준 미확정 → 박스가 없으면 UNKNOWN |
| 항목 번호 상하위 순서 | 들여쓰기 1칸 단위 미확정 → UNKNOWN |

### 알려진 제한사항

- 역할별 기준이 필요한 항목은 Inspector 의 **추정 역할**에 의존한다. 신뢰도가 기준(0.6)에 겨우 걸치는 문단은
  역할이 틀릴 수 있으므로 해당 FAIL 은 `auto_if_role_confirmed`(확인 후 수정)로 표시된다.
- 글꼴은 한글 언어 슬롯만, 장평·자간은 한글·영문 슬롯만 비교한다(나머지 슬롯 기준이 명세에 없음).
- 혼재 문단은 공백이 아닌 글자를 가진 모든 글자 모양을 검사한다. 공백만 있는 run 은 검사하지 않지만
  수정 계획의 `정의 복제 필요` 판단에는 포함한다.
- 문단 간 간격은 본문 최상위 문단만 대상이다(표 셀·머리말·각주·글상자 제외). 주석·캡션·개체·계층 미정 항목이 끼면
  이후 문단의 소속이 모호하다고 보고 판단 불가로 둔다(5.4). 기존 빈 문단은 삭제하지 않고 `observation` 으로 보고한다.
- 실제 하위 항목을 거쳐 상위로 복귀하는 경우(예 `가.` → `1)` → `나.`)는 5.3·5.9 대로 14pt 로 판정하되, 6장 미확정 사항이라 `interpretation_note` 를 붙인다.
- 줄 수는 한글이 저장 시 기록한 `hp:linesegarray` 캐시로 판단한다. 작성 프로그램이 한글이 아니면 신뢰하지 않는다.
  캐시의 줄 시작 위치(horzpos)에는 내어쓰기가 반영되지 않으므로(샘플 관찰) 정렬 판단에는 문단 모양의 들여쓰기 값을 쓴다.
- 글꼴 명세의 '휴면명조'는 '휴먼명조'의 오기로 보고 휴먼명조로 검사한다.
- 날짜·시간 감지는 정규식 기반이다. 4자리 연도(19xx/20xx)와 'YY 형식만 날짜로 본다.
- 자간을 0%로 바꾸면 줄·쪽 나눔이 달라질 수 있다(어절 단위 조정 방식이 미확정이라 고려하지 않음).
- `config/format_standard.json` 은 FORMAT_SPEC.md 와 수동으로 맞춰야 한다. 명세를 바꾸면 설정도 함께 바꾼다.

---

## 3. Formatter (서식 수정기)

Checker 의 수정 계획(fix_plan) 중 **자동 수정**(`auto_actions`)과 **역할이 확인된 분석 후 수정**(`role_based_actions`)을
HWPX 복제본에 적용하고, 수정본을 다시 분석·검사해 검증한다. 검증에 실패하면 수정본을 만들지 않는다.

```
python3 scripts/run_fix.py samples/문서.hwpx [--doc-type A|B]          # 결과 폴더까지 한 번에
python3 src/formatter.py samples/문서.hwpx --out-dir output/문서_fix    # 수정기만
```

### 지원하는 수정 동작 (src/hwpx_format/ops.py)

| 동작 | 바꾸는 XML | 비고 |
|---|---|---|
| 자간 / 장평 | `hh:charPr/hh:spacing`, `hh:ratio` 의 수정 계획에 적힌 언어 슬롯(한글·영문) | |
| 글꼴 | `hh:charPr/hh:fontRef@hangul` | 문서에 등록된 글꼴일 때만. 미등록이면 적용하지 않고 보고 |
| 줄 간격 | `hh:paraPr` 안 모든 `hh:lineSpacing`(switch case·default) | |
| 문단 위 간격 | `hh:paraPr` 안 `hc:prev` — case 는 pt×100, default 는 그 2배 | 목표값 설정, 누적 없음 |
| 정렬 | `hh:paraPr/hh:align@horizontal` | |
| 내어쓰기 | `hh:paraPr` 안 `hc:intent` — case 는 pt×100(음수), default 는 그 2배 | 왼쪽 여백은 그대로. 폭은 글자 수 근사 |
| 용지 크기 | `hp:secPr/hp:pagePr@width,height` (A4 = 59528×84188) | 가로/세로 방향은 유지 |

동작을 추가·제거하려면 `ops.py` 의 함수와 `DEF_OPS`/`SECTION_OPS` 항목만 바꾼다.

### 수정 방식
- 기존 `charPr`/`paraPr` 정의는 고치지 않는다. 대상 문단이 쓰는 정의를 **복제해 새 id 로 추가**하고, 대상 문단(또는 그 run)만 새 id 를 참조시킨다.
  같은 정의라도 적용할 동작 묶음이 다르면 묶음별로 따로 복제한다. 그래서 대상이 아닌 문단·스타일의 서식은 바뀌지 않는다.
- XML 은 바이트 단위로 해당 속성값만 바꾼다(네임스페이스 접두어·따옴표·선언문 등 나머지 바이트 그대로). ZIP 엔트리 순서·압축 방식도 원본과 같다.

### 검증 (하나라도 실패하면 수정본 폐기, 종료 코드 2)
패키지 엔트리·순서·압축 동일 / 바꾼 파트(header·section) 외 바이트 동일 / mimetype 첫 엔트리 무압축 / 수정본 Inspector 검증 통과 /
문단 수·순서·텍스트·개체 동일 / 표 구조·셀 병합·그림 동일 / 대상 아닌 문단의 서식 변화 없음 / 적용한 항목이 재검사에서 PASS / PASS 였던 항목의 FAIL 회귀 없음.

### 수정하지 않는 것
- 검사만 항목(날짜·시간·번호·기호·제목 박스): 글자를 바꿔야 하므로 보고만 한다.
- 판단 불가(UNKNOWN)·검사 제외(SKIP)·분석만 항목.

### 알려진 제한사항
- 줄 배치 캐시(`hp:linesegarray`)는 원본 값 그대로 둔다. 자간·줄 간격·문단 간격이 바뀌면 실제 줄·쪽 나눔이 달라질 수 있으므로 한글에서 열어 확인해야 한다.
- 개발 환경에 한글 프로그램이 없어, 수정본을 한글에서 연 화면 확인은 아직 하지 못했다(구조·재검사 검증만 수행).
- 자간·장평은 수정 계획에 적힌 언어 슬롯(한글·영문)만 바꾼다. 한자·일본어 등 나머지 슬롯은 명세 기준이 없어 그대로 둔다.
- 미리보기(`Preview/PrvImage.png`)는 갱신하지 않는다.
- 내어쓰기 폭은 글자 수 근사라 실제 글꼴 폭과 1pt 안팎 차이가 날 수 있다. 작성자가 일부러 크게 준 내어쓰기(예: "입찰건명 : " 뒤에 맞춘 109.9pt)도 명세대로 번호·기호 폭으로 바뀐다.

---

## 4. Mattermost 봇과 서버 배포

### 봇 사용법 (Mattermost)
- 봇 `@letshwp` 에게 hwpx 를 첨부해 DM 을 보내거나, 채널에서 `@letshwp` 멘션 + 첨부.
- 메시지 없음/`수정` → 서식 수정. 스레드 답장에 **수정본 hwpx 하나만** 첨부하고, 메시지에는 change_report.md 의 "수정한 내용"과
  "직접 확인 필요" 항목만 싣는다(검증 통과·적합 집계는 생략, 표가 길면 항목별 건수만). `검사`/`테스트` → 검사만(check_report.txt).
  `A`/`B` 를 함께 쓰면 문서 유형 지정(예: `수정 B`). `도움말` → 안내.
- 봇은 저장소 실행기(scripts/run_fix.py, run_test.py)를 그대로 실행한다. 검증 실패 시 수정본 없이 실패 사유만 답한다.
- 첨부·결과는 서버 `<base>/shared/data/inbox|output/` 에 저장되고 30일 후 지워진다(LETSHWP_KEEP_DAYS).

### 구성
| 파일 | 역할 |
|---|---|
| src/mmbot/websocket.py | 표준 라이브러리 WebSocket 클라이언트(이벤트 수신) |
| src/mmbot/client.py | Mattermost REST API v4(파일 받기·올리기, 글 쓰기) |
| src/mmbot/handler.py | 메시지 해석, 실행기 실행, 답장 구성(실행기 출력 파일만 사용) |
| src/mmbot/bot.py | 이벤트 루프, 작업 큐(한 번에 하나), 재연결, status.json |
| scripts/run_bot.py | 봇 실행(`--env 설정파일`), `--check` 연결 확인 |
| scripts/deploy.py | 배포·상태·롤백·로그·토큰 저장 |
| deploy/ | deploy.json(서버 정보), systemd 유닛, bot.env.example |

### 배포 (`python3 scripts/deploy.py ...`, Claude 에게 "배포해줘")
```
deploy            # 로컬 테스트 → git archive 업로드 → 서버 테스트 → current 교체 → 재시작 → 연결 확인(실패 시 자동 복구)
status | rollback | logs -n 50
setup             # 최초 1회: 폴더·설정 파일·systemd 서비스(봇 실행 계정 letshwp 생성)
join-bot          # 봇 계정을 팀·채널에 추가(봇 계정은 시스템 콘솔 > 통합 > 봇 계정에서 생성)
printf %s "<토큰>" | python3 scripts/deploy.py set-token
set-env MM_URL=http://127.0.0.1:8065
```
서버 폴더: `<base>/releases/<시각>_<커밋>/`, `<base>/current`, `<base>/shared/bot.env`(600), `<base>/shared/data/`, `<base>/deploy_history.log`.
배포본에는 .git 이 없으므로 `DEPLOY_VERSION.json` 의 커밋이 run_info.json 의 코드 버전으로 기록된다.

---

## 5. 테스트

```
python3 -m unittest discover -s tests -v                       # Inspector + Checker + Formatter 전체
HWPX_SAMPLE_DIR=samples python3 -m unittest tests.test_inspector   # 실제 샘플 포함
```
