"""HWPX ZIP 컨테이너 읽기 전용 접근.

HWPX 패키지 구조(OCF 기반, 샘플로 확인):
  mimetype                 'application/hwp+zip' (무압축, 첫 엔트리)
  version.xml              hv:HCFVersion (작성 프로그램/OWPML 버전)
  META-INF/container.xml   ocf:rootfile -> Contents/content.hpf
  META-INF/manifest.xml
  Contents/content.hpf     opf:package (metadata, manifest, spine)
  Contents/header.xml      hh:head (서식 정의)
  Contents/section{N}.xml  hs:sec (본문)
  BinData/*                이미지/OLE 등 바이너리
  Preview/PrvText.txt, Preview/PrvImage.png, settings.xml, Scripts/*

원본 파일은 zipfile 의 'r' 모드로만 열며 어떤 쓰기도 하지 않는다.
분석 전후의 SHA-256 을 비교해 원본 불변을 검증한다.
"""

import hashlib
import posixpath
import zipfile

from .xmlutil import parse_xml, ln, children, child

EXPECTED_MIMETYPE = "application/hwp+zip"
MAX_XML_PART_BYTES = 200 * 1024 * 1024  # 압축 해제 크기 상한(비정상 파일 방어)


class HwpxFormatError(ValueError):
    pass


def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class HwpxPackage:
    """읽기 전용 HWPX 패키지. with 문으로 사용한다."""

    def __init__(self, path):
        self.path = str(path)
        self._zip = None
        self.warnings = []

    def __enter__(self):
        try:
            self._zip = zipfile.ZipFile(self.path, "r")
        except zipfile.BadZipFile as e:
            raise HwpxFormatError("ZIP 형식이 아닙니다: %s" % e)
        return self

    def __exit__(self, *exc):
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    # ------------------------------------------------------------------
    def names(self):
        return self._zip.namelist()

    def has(self, name):
        try:
            self._zip.getinfo(name)
            return True
        except KeyError:
            return False

    def read_bytes(self, name):
        info = self._zip.getinfo(name)
        if info.file_size > MAX_XML_PART_BYTES:
            raise HwpxFormatError("파트 크기가 상한을 초과합니다: %s (%d bytes)" % (name, info.file_size))
        return self._zip.read(name)

    def read_xml(self, name):
        return parse_xml(self.read_bytes(name))

    # ------------------------------------------------------------------
    def describe(self):
        """패키지 구조와 매니페스트를 해석해 dict 로 반환."""
        infos = self._zip.infolist()
        entries = [
            {
                "name": i.filename,
                "size": i.file_size,
                "compressed_size": i.compress_size,
                "compress_type": "STORED" if i.compress_type == zipfile.ZIP_STORED else "DEFLATED"
                if i.compress_type == zipfile.ZIP_DEFLATED else str(i.compress_type),
            }
            for i in infos
        ]

        # mimetype
        mimetype = None
        if self.has("mimetype"):
            mimetype = self.read_bytes("mimetype").decode("ascii", "replace").strip()
        if mimetype != EXPECTED_MIMETYPE:
            raise HwpxFormatError("mimetype 이 '%s' 가 아닙니다: %r" % (EXPECTED_MIMETYPE, mimetype))
        mimetype_first = bool(infos) and infos[0].filename == "mimetype"
        mimetype_stored = self._zip.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        if not mimetype_first:
            self.warnings.append("mimetype 이 ZIP 첫 엔트리가 아닙니다(OCF 권고 위반).")
        if not mimetype_stored:
            self.warnings.append("mimetype 이 무압축(STORED)이 아닙니다(OCF 권고 위반).")

        # version.xml
        version = None
        if self.has("version.xml"):
            v = self.read_xml("version.xml")
            version = dict(v.attrib)

        # container.xml -> rootfile
        rootfile = "Contents/content.hpf"
        rootfiles = []
        if self.has("META-INF/container.xml"):
            c = self.read_xml("META-INF/container.xml")
            for rf in c.iter():
                if ln(rf.tag) == "rootfile":
                    rootfiles.append({"full-path": rf.get("full-path"), "media-type": rf.get("media-type")})
            for rf in rootfiles:
                if rf["media-type"] == "application/hwpml-package+xml":
                    rootfile = rf["full-path"]
                    break
        else:
            self.warnings.append("META-INF/container.xml 이 없습니다. 기본 경로 Contents/content.hpf 를 사용합니다.")

        if not self.has(rootfile):
            raise HwpxFormatError("패키지 문서(%s)가 없습니다." % rootfile)

        opf = self.read_xml(rootfile)
        base = posixpath.dirname(rootfile)

        def resolve(href):
            # 샘플에서 href 는 패키지 루트 기준("Contents/header.xml")으로 기록되어 있다.
            # 루트 기준 경로가 존재하면 그것을, 아니면 hpf 기준 상대경로를 시도한다.
            if self.has(href):
                return href
            alt = posixpath.normpath(posixpath.join(base, href))
            return alt if self.has(alt) else href

        metadata = {}
        manifest = []
        spine = []
        md = child(opf, "metadata")
        if md is not None:
            for m in md:
                name = ln(m.tag)
                if name == "meta":
                    metadata[m.get("name")] = (m.text or "").strip()
                else:
                    metadata[name] = (m.text or "").strip()
        mf = child(opf, "manifest")
        if mf is not None:
            for it in children(mf, "item"):
                manifest.append({
                    "id": it.get("id"),
                    "href": resolve(it.get("href") or ""),
                    "media-type": it.get("media-type"),
                    "isEmbeded": it.get("isEmbeded"),
                })
        sp = child(opf, "spine")
        if sp is not None:
            spine = [ir.get("idref") for ir in children(sp, "itemref")]

        by_id = {m["id"]: m for m in manifest}
        header_href = None
        section_hrefs = []
        for idref in spine:
            item = by_id.get(idref)
            if not item:
                continue
            href = item["href"]
            fname = posixpath.basename(href)
            if fname == "header.xml" and header_href is None:
                header_href = href
            elif fname.startswith("section") and fname.endswith(".xml"):
                section_hrefs.append(href)
        if header_href is None and self.has("Contents/header.xml"):
            header_href = "Contents/header.xml"
            self.warnings.append("spine 에 header 가 없어 Contents/header.xml 을 사용합니다.")
        if not section_hrefs:
            section_hrefs = sorted(
                (n for n in self.names() if posixpath.dirname(n) == "Contents"
                 and posixpath.basename(n).startswith("section") and n.endswith(".xml")),
                key=lambda n: int("".join(ch for ch in posixpath.basename(n) if ch.isdigit()) or 0),
            )
            self.warnings.append("spine 에서 section 을 찾지 못해 파일명 순서로 대체합니다.")

        bin_items = {m["id"]: m for m in manifest if m["href"].startswith("BinData/")}

        return {
            "mimetype": mimetype,
            "mimetype_first_entry": mimetype_first,
            "mimetype_stored": mimetype_stored,
            "version": version,
            "rootfiles": rootfiles,
            "package_document": rootfile,
            "metadata": metadata,
            "manifest": manifest,
            "spine": spine,
            "header_part": header_href,
            "section_parts": section_hrefs,
            "binary_items": bin_items,
            "entries": entries,
        }
