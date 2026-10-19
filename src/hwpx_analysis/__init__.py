"""HWPX 문서 서식 분석 패키지 (읽기 전용).

모듈 구성
- package    : ZIP 컨테이너를 읽기 전용으로 열고 매니페스트·스파인을 해석
- header     : Contents/header.xml 의 서식 정의(글꼴, 글자/문단 모양, 스타일 등)
- section    : Contents/sectionN.xml 본문 순회(문단, 표, 그림, 도형, 구역 설정)
- classifier : 문단 역할 분류기(근거·신뢰도 포함)
- stats      : 서식 종류·빈도 집계
- report     : 사람이 읽는 텍스트 보고서 생성

이 패키지는 원본 파일에 어떠한 쓰기도 하지 않는다.
"""

ANALYZER_VERSION = "0.1.0"
