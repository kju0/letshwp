"""HWPUNIT 단위 변환.

OWPML 의 길이 단위 HWPUNIT 은 1/7200 inch 이다.
- 1pt  = 100 HWPUNIT  (1pt = 1/72 inch)
- 1mm  = 7200 / 25.4 HWPUNIT ≈ 283.465
글자 크기(charPr@height)도 HWPUNIT 이므로 1000 = 10pt 이다.
"""

HWPUNIT_PER_INCH = 7200
HWPUNIT_PER_PT = 100
MM_PER_INCH = 25.4


def to_pt(value):
    if value is None:
        return None
    return round(value / HWPUNIT_PER_PT, 2)


def to_mm(value):
    if value is None:
        return None
    return round(value * MM_PER_INCH / HWPUNIT_PER_INCH, 2)


# 용지 판별 표(mm). 허용 오차 내에서 치수가 일치할 때만 이름을 부여한다.
PAPER_SIZES_MM = {
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
    "B4(JIS)": (257.0, 364.0),
    "B5(JIS)": (182.0, 257.0),
    "Letter": (215.9, 279.4),
    "Legal": (215.9, 355.6),
}


def detect_paper(width_mm, height_mm, tolerance_mm=1.0):
    if width_mm is None or height_mm is None:
        return None
    short, long_ = sorted((width_mm, height_mm))
    for name, (w, h) in PAPER_SIZES_MM.items():
        if abs(short - w) <= tolerance_mm and abs(long_ - h) <= tolerance_mm:
            return name
    return None
