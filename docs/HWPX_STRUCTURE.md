# HWPX 구조 분석 노트 (분석 엔진 근거)

한글 2020+/Hancom Office 12(Linux)로 저장된 샘플 HWPX 5종(보고서, 제안서, 양식, 신청서, 설치 가이드)을 직접 풀어 확인한 구조와
OWPML(KS X 6101) 요소 의미를 대조한 결과다. 분석 엔진(`src/hwpx_analysis`)은 여기 적힌 해석만 사용한다.

## 1. 패키지(ZIP/OCF)

| 파트 | 역할 | 비고 |
|---|---|---|
| `mimetype` | `application/hwp+zip` | 첫 엔트리·무압축(STORED) — 5종 모두 확인 |
| `version.xml` | `hv:HCFVersion` 작성 프로그램·`xmlVersion`(1.5) | |
| `META-INF/container.xml` | `ocf:rootfile` → `Contents/content.hpf` | |
| `Contents/content.hpf` | `opf:metadata`/`manifest`/`spine` | href 는 패키지 루트 기준 경로 |
| `Contents/header.xml` | `hh:head` — 서식 정의(refList) | |
| `Contents/section{N}.xml` | `hs:sec` — 본문 | spine 순서가 구역 순서 |
| `BinData/*` | 이미지·OLE | manifest id 가 `hc:img@binaryItemIDRef` 와 연결 |
| `Preview/*`, `settings.xml`, `Scripts/*` | 미리보기·캐럿 위치·스크립트 | 분석 대상 아님 |

## 2. 서식 참조 모델

- `hp:p@paraPrIDRef` → `hh:paraPr`, `hp:run@charPrIDRef` → `hh:charPr` 는 **완결된 속성 집합**을 가리킨다.
  스타일(`hp:p@styleIDRef`)은 출발점일 뿐이므로 실제 서식 = 참조된 정의 그대로. 스타일 상속 계산이 필요 없다.
- 샘플 s5 에서 스타일 이름 `제목 1.` 이 ※ 주석 문단에 쓰이는 등 **스타일 이름은 역할의 신뢰할 근거가 아니다**.

### charPr (글자 모양)
- `@height` 글자 크기(HWPUNIT, 1000 = 10pt), `hh:fontRef` 7개 언어 슬롯(hangul/latin/hanja/japanese/other/symbol/user)별
  `hh:fontface[@lang]/hh:font@id` 참조.
- `hh:ratio` 장평 %, `hh:spacing` 자간 %, `hh:relSz` 상대 크기 %, `hh:offset` 글자 위치 %.
- `hh:bold`, `hh:italic` 은 **요소 존재 여부**로 표현(속성 아님).

### paraPr (문단 모양)
- `hh:align@horizontal` JUSTIFY/LEFT/RIGHT/CENTER/DISTRIBUTE…
- `hh:heading@type` NONE/OUTLINE/NUMBER/BULLET, `@level` 은 **0부터**(개요 1 = level 0).
  OUTLINE 번호 모양은 `hp:secPr@outlineShapeIDRef` 가 가리키는 `hh:numbering` 의 `paraHead[level+1]`(예: `^1.`).
  자동 번호 문자열은 `hp:t` 에 나타나지 않는다.
- `hh:margin`(intent=들여쓰기, left, right, prev=문단 위, next=문단 아래)과 `hh:lineSpacing` 은 한글 저장 파일에서
  **`hp:switch` 안에** 기록된다.
  ```xml
  <hp:switch>
    <hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar"> margin/lineSpacing </hp:case>
    <hp:default> margin/lineSpacing </hp:default>
  </hp:switch>
  ```
  관측(5종 전수): margin/lineSpacing 을 감싼 switch 259개 중 183개에서 default 의 margin 값이 case 와 다르며,
  183개 **모두 정확히 2배**다. 줄 간격은 259개 모두 동일.
  판단: `hp:switch` 는 "이해하는 첫 case 를 쓰고 없으면 default" 규칙이며 한글은 HwpUnitChar 를 지원하므로 case 값이
  화면에 적용되는 값이다. 한글 기본 '본문' 스타일 왼쪽 여백 15pt 가 case 값 1500 과 일치(s1 paraPr 2)하는 것으로 교차 확인.
  → 분석기는 case 를 채택하고 default 원값을 `margin_switch_default_raw` 로 병기한다.
- `hp:switch` 는 한 paraPr 안에 여러 개 올 수 있고 margin 외 요소도 감싼다(5종 전수 확인):

  | 감싼 요소 | case required-namespace | 개수 | 의미(관측) |
  |---|---|---|---|
  | `hh:margin` + `hh:lineSpacing` | `2016/HwpUnitChar` | 259 | 위 설명 |
  | `hh:heading` | `2016/paragraph` | 9 | case=OUTLINE 8~10수준, default=NONE (구형 리더는 개요 8수준 이상 미지원으로 추정) |
  | `hh:tabItem` (tabPr) | `2016/HwpUnitChar` | 93 | case 에만 `unit="HWPUNIT"` |

  → 분석기는 모든 하위 요소를 "직계 자식 + 각 switch 에서 선택된 분기"로 조회한다(`xmlutil.effective_children`).
  처리 위치가 정의되지 않은 switch(예: charPr 내부)나 이해하지 못하는 namespace 는 경고로 남긴다.

## 3. 본문 트리

```
hs:sec
 └ hp:p
    ├ hp:run
    │  ├ hp:t            텍스트 (혼합 내용: hp:tab, hp:lineBreak, hp:fwSpace, hp:titleMark …)
    │  ├ hp:secPr        구역 설정 — 첫 문단 첫 run. hp:pagePr(width/height/landscape) + hp:margin
    │  ├ hp:ctrl         colPr / header / footer / footNote / endNote / pageNum / fieldBegin …
    │  ├ hp:tbl          hp:tr > hp:tc(hp:subList > hp:p…, hp:cellAddr, hp:cellSpan, hp:cellSz, hp:cellMargin)
    │  ├ hp:pic          hc:img@binaryItemIDRef, hp:orgSz / hp:curSz / hp:sz / hp:pos
    │  ├ hp:rect …       도형. hp:drawText > hp:subList 에 글상자 문단
    │  └ hp:container    묶음 개체(내부에 pic/도형)
    └ hp:linesegarray    레이아웃 캐시 — 서식 분석에 사용하지 않음
```

- `hp:p@id` 는 중복값(예: 2147483648)이 흔해 식별자로 쓸 수 없다. 분석기는 XML 위치 경로(`sec0/p12/tbl0/r1c2/p0`)를 식별자로 쓴다.
- 표 셀 여백: `hp:tc@hasMargin=0` 이면 표의 `hp:inMargin`, 1 이면 셀의 `hp:cellMargin`.
- 캡션: 규격상 `hp:caption`(표/그림 하위)이 있으나 **샘플 5종 모두 캡션을 일반 문단("<그림 1> …")으로 작성**했다.
  → 분류기는 두 경우를 모두 처리한다.
- 표지 제목이 1열 표(제목 박스, s2/s3) 또는 글상자(s5)에 들어가는 경우가 관측됨.

## 4. 해석 미확정 항목

| 항목 | 상태 |
|---|---|
| `hp:pagePr@landscape` (WIDELY/NARROWLY) | 5종 모두 A4 세로 치수에 `WIDELY`. 의미 미확인 → 원값만 보고, 방향은 치수로 판단 |
| `hp:titleMark@ignore` | 차례 항목 안에서 관측. 텍스트로 취급하지 않음 |
| 언어 슬롯별 실제 글자 배정 | 어떤 문자가 어느 슬롯 글꼴을 쓰는지 규칙 미확인 → 대표값은 hangul 슬롯 |

## 5. 분석 결과 JSON 주요 키

| 키 | 내용 |
|---|---|
| `source` | 경로, SHA-256, 분석 후 불변 여부 |
| `package` | mimetype, manifest, spine, 엔트리 목록 |
| `document.sections[]` | 용지·여백·단·쪽 번호·머리말/꼬리말 |
| `format_definitions` | header.xml 전체 해석(글꼴, charPr, paraPr, 스타일, 테두리/배경, 번호) |
| `paragraphs[]` | 문단별 경로·문맥·텍스트·대표 글자/문단 서식·run·자동번호·항목기호·**role(근거·신뢰도·후보)** |
| `tables[]` | 행/열, 셀(병합·크기·여백·배경·세로정렬·문단), 격자 검증, **표 내부 서식 요약** |
| `pictures[]`, `shapes[]`, `other_objects[]`, `notes[]` | 개체 정보 |
| `classification` | 본문 기준값(크기·글꼴·분포), 임계값, 역할 라벨 |
| `statistics` | 글자 수/문단 수 기준 빈도, 영역별·역할별 분포, 서식 조합(시그니처), 정의 사용 현황 |
| `verification` | 문단/표/그림 방문 수 = XML 개수, hp:t 텍스트 대조, 표 격자, 미처리 요소 |
| `support_matrix` | 항목별 신뢰 수준(정확/계산/추정/미지원) |
