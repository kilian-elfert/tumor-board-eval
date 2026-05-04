#!/usr/bin/env python3
"""Generate a printable PDF version of the study informed consent form (v2).

Style refresh inspired by the Fraunhofer SATURN "Teilnahmebedingungen" PDF:
  * decimal-numbered headings (1, 1.1, 2, 2.1, ...)
  * accent rule only beneath top-level H1 headings
  * justified body text
  * page-counter footer "Seite X von N"
  * sub-brand "IMIBE - Universitätsklinikum Essen" in continuation header
  * declaration block as a clean white section with a simple signature line
    (no tinted box)

Writes to exports/Einwilligung_Studienteilnahme_v2.pdf by default and does
not overwrite the original PDF produced by export_consent_pdf.py.

Usage:
    uv run export_consent_pdf_v2.py
    uv run export_consent_pdf_v2.py --output exports/Einwilligung_v2.pdf
"""

from __future__ import annotations

import argparse
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, KeepTogether, ListFlowable, ListItem,
    PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

try:
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPDF
except ImportError:  # pragma: no cover
    svg2rlg = None
    renderPDF = None


# ---------------------------------------------------------------------------
# styling
# ---------------------------------------------------------------------------

H1_COLOR = colors.HexColor("#1a4f8a")        # app primary blue
H2_COLOR = colors.HexColor("#1a2226")        # near-black H2
BODY     = colors.HexColor("#1a2226")
MUTED    = colors.HexColor("#6e7a80")
ACCENT   = colors.HexColor("#1a4f8a")
RULE     = colors.HexColor("#9aa7ad")
SIG_TEAL = colors.HexColor("#1a4f8a")        # app blue for signature box
SIG_BG   = colors.HexColor("#f3f6fb")        # very light tint of app blue

SUB_BRAND = "IMIBE \u2013 Universitätsklinikum Essen"
DOC_TITLE = "Einverständniserklärung zur Studienteilnahme"


class HeadingRule(Flowable):
    """Thin accent rule that always spans the available frame width."""

    def __init__(self, thickness: float = 0.8, color=ACCENT,
                 space_below: float = 6):
        super().__init__()
        self._t = thickness
        self._c = color
        self._sb = space_below
        self._w = 0

    def wrap(self, aw, _ah):
        self._w = aw
        return aw, self._t + self._sb

    def draw(self):
        c = self.canv
        c.setStrokeColor(self._c)
        c.setLineWidth(self._t)
        c.line(0, self._sb, self._w, self._sb)


def build_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["Normal"]
    base.fontName = "Helvetica"
    base.fontSize = 10
    base.leading = 14
    base.textColor = BODY

    return {
        "title": ParagraphStyle(
            "Title", parent=base, fontName="Helvetica-Bold",
            fontSize=22, leading=26, textColor=BODY,
            spaceAfter=4, alignment=TA_LEFT,
        ),
        "subtitle": ParagraphStyle(
            "Subtitle", parent=base, fontSize=10.5, leading=14,
            textColor=BODY, spaceAfter=10, alignment=TA_LEFT,
        ),
        "h1": ParagraphStyle(
            "H1", parent=base, fontName="Helvetica-Bold",
            fontSize=15, leading=19, textColor=H1_COLOR,
            spaceBefore=18, spaceAfter=2,
            keepWithNext=True,
        ),
        "h2": ParagraphStyle(
            "H2", parent=base, fontName="Helvetica-Bold",
            fontSize=11, leading=14, textColor=H2_COLOR,
            spaceBefore=10, spaceAfter=2,
            keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "Body", parent=base, fontSize=10, leading=14,
            textColor=BODY, alignment=TA_JUSTIFY, spaceAfter=4,
        ),
        "li": ParagraphStyle(
            "LI", parent=base, fontSize=10, leading=14,
            textColor=BODY, alignment=TA_LEFT,
        ),
        "small": ParagraphStyle(
            "Small", parent=base, fontSize=8.5, leading=11,
            textColor=MUTED,
        ),
        "sign_label": ParagraphStyle(
            "SignLabel", parent=base, fontSize=9, leading=11,
            textColor=BODY,
        ),
    }


# ---------------------------------------------------------------------------
# numbered section helpers
# ---------------------------------------------------------------------------

class Outline:
    """Auto-numbering helper for top-level (`1`, `2`) and sub (`1.1`) sections."""

    def __init__(self) -> None:
        self.h1 = 0
        self.h2 = 0

    def chapter(self, title: str, styles: dict) -> list:
        self.h1 += 1
        self.h2 = 0
        text = f"<b>{self.h1}</b>&nbsp;&nbsp;{title}"
        return [
            Paragraph(text, styles["h1"]),
            HeadingRule(thickness=0.8, color=H1_COLOR, space_below=4),
        ]

    def sub(self, title: str, body, styles: dict) -> list:
        self.h2 += 1
        head_text = f"{self.h1}.{self.h2}&nbsp;&nbsp;{title}"
        head = Paragraph(head_text, styles["h2"])
        if isinstance(body, str):
            body_flow: Flowable | list = Paragraph(body, styles["body"])
        elif isinstance(body, list) and body and isinstance(body[0], str):
            body_flow = bullet_list(body, styles)
        else:
            body_flow = body
        return [KeepTogether([head, body_flow])]


def bullet_list(items: list[str], styles: dict) -> ListFlowable:
    return ListFlowable(
        [ListItem(Paragraph(t, styles["li"]), leftIndent=12, value="\u2022")
         for t in items],
        bulletType="bullet", bulletColor=ACCENT, bulletFontSize=8,
        leftIndent=14, spaceBefore=2, spaceAfter=4,
    )


# ---------------------------------------------------------------------------
# signature block (clean two-column variant matching SATURN)
# ---------------------------------------------------------------------------

def _signature_line(width: float) -> Flowable:
    class _Line(Flowable):
        def wrap(self, _aw, _ah):
            return width, 0.5
        def draw(self):
            c = self.canv
            c.setStrokeColor(RULE)
            c.setLineWidth(0.6)
            c.line(0, 0, width, 0)
    return _Line()


def signature_block(role: str, styles: dict, frame_w: float = 0) -> Table:
    """Three-column signature block matching v1 layout."""
    col_widths = [4.7 * cm, 5.6 * cm, 6.2 * cm]
    inner_pad = 6

    label = Paragraph(f"<b>{role}</b>", styles["body"])
    label_row = [label, "", ""]
    sig_row = [_signature_line(w - 2 * inner_pad) for w in col_widths]
    cap_row = [
        Paragraph("Ort, Datum", styles["sign_label"]),
        Paragraph("Name in Druckbuchstaben", styles["sign_label"]),
        Paragraph("Unterschrift", styles["sign_label"]),
    ]

    t = Table(
        [label_row, sig_row, cap_row],
        colWidths=col_widths,
        rowHeights=[0.7 * cm, 1.7 * cm, 0.55 * cm],
        hAlign="LEFT",
    )
    t.setStyle(TableStyle([
        ("SPAN",          (0, 0), (-1, 0)),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING",    (0, 0), (-1, 0), 0),
        ("LEFTPADDING",   (0, 0), (-1, -1), inner_pad),
        ("RIGHTPADDING",  (0, 0), (-1, -1), inner_pad),
        ("TOPPADDING",    (0, 1), (-1, 1), 6),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 0),
        ("VALIGN",        (0, 1), (-1, 1), "BOTTOM"),
        ("VALIGN",        (0, 2), (-1, 2), "TOP"),
        ("TOPPADDING",    (0, 2), (-1, 2), 2),
    ]))
    return t


# ---------------------------------------------------------------------------
# page chrome
# ---------------------------------------------------------------------------

LOGO_DIR = Path(__file__).resolve().parent / "static"
LOGO_FILES = (
    ("logo_ume.webp",),
    ("logo_ude.jpg", "logo_ude.png", "logo_ude.svg"),
    ("logo_dfg.svg", "logo_dfg.png"),
    ("Wispermed_Logo_de.png",),
)


def _load_logo(names: tuple[str, ...]):
    for name in names:
        p = LOGO_DIR / name
        if not p.is_file():
            continue
        if p.suffix.lower() == ".svg":
            if svg2rlg is None:
                continue
            try:
                drw = svg2rlg(str(p))
                if drw is None:
                    continue
                return ("svg", drw, drw.width, drw.height)
            except Exception:
                continue
        else:
            try:
                img = ImageReader(str(p))
                iw, ih = img.getSize()
                return ("raster", img, iw, ih)
            except Exception:
                continue
    return None


def _draw_logos(canvas: Canvas, page_w: float, header_h: float, header_y: float,
                margin_x: float) -> None:
    avail_w = page_w - 2 * margin_x
    max_h = 1.4 * cm
    h_pad = 0.4 * cm

    logos = []
    for names in LOGO_FILES:
        info = _load_logo(names)
        if info is None:
            continue
        kind, payload, iw, ih = info
        if iw <= 0 or ih <= 0:
            continue
        logos.append((kind, payload, iw, ih))
    if not logos:
        return

    n = len(logos)
    col_w = avail_w / n
    for i, (kind, payload, iw, ih) in enumerate(logos):
        slot_w = col_w - 2 * h_pad
        scale = min(slot_w / iw, max_h / ih)
        w = iw * scale
        h = ih * scale
        cx = margin_x + (i + 0.5) * col_w
        x = cx - w / 2
        y = header_y + (header_h - h) / 2
        if kind == "raster":
            canvas.drawImage(payload, x, y, width=w, height=h,
                             mask="auto", preserveAspectRatio=True)
        else:
            drw = payload
            canvas.saveState()
            canvas.translate(x, y)
            canvas.scale(w / drw.width, h / drw.height)
            renderPDF.draw(drw, canvas, 0, 0)
            canvas.restoreState()


class NumberedCanvas(Canvas):
    """Two-pass canvas that prints `Seite X von N` in the footer."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_pages: list[dict] = []

    def showPage(self) -> None:
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved_pages)
        for state in self._saved_pages:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total: int) -> None:
        page_w, _ = A4
        self.saveState()
        # thin rule above footer
        self.setStrokeColor(RULE)
        self.setLineWidth(0.4)
        self.line(2 * cm, 1.5 * cm, page_w - 2 * cm, 1.5 * cm)
        # left: sub-brand on continuation pages only
        self.setFillColor(MUTED)
        self.setFont("Helvetica", 8)
        # center: doc title
        self.drawCentredString(page_w / 2, 1.05 * cm, DOC_TITLE)
        # right: Seite X von N
        self.drawRightString(page_w - 2 * cm, 1.05 * cm,
                             f"Seite {self._pageNumber} von {total}")
        self.restoreState()


def _draw_chrome(canvas: Canvas, doc) -> None:
    canvas.saveState()
    page_w, page_h = A4
    margin_x = 2 * cm

    # full logo header on every page (matches v1)
    header_h = 2.4 * cm
    header_y = page_h - header_h - 0.5 * cm  # extra top padding above logos
    _draw_logos(canvas, page_w, header_h, header_y, margin_x)

    # accent rule under the header
    canvas.setStrokeColor(ACCENT)
    canvas.setLineWidth(1.2)
    canvas.line(margin_x, header_y - 0.05 * cm,
                page_w - margin_x, header_y - 0.05 * cm)

    canvas.restoreState()


# ---------------------------------------------------------------------------
# document build
# ---------------------------------------------------------------------------

def build_doc(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    styles = build_styles()

    doc = BaseDocTemplate(
        str(output), pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=3.4 * cm, bottomMargin=2.0 * cm,
        title=DOC_TITLE,
        author="Kilian Elfert, IMIBE \u2013 Universitätsklinikum Essen",
    )
    frame = Frame(
        doc.leftMargin, doc.bottomMargin,
        doc.width, doc.height - 0.6 * cm,
        leftPadding=0, rightPadding=0, topPadding=0.4 * cm, bottomPadding=0,
    )
    doc.addPageTemplates([
        PageTemplate(id="main", frames=[frame], onPage=_draw_chrome),
    ])

    o = Outline()
    story: list = []

    # title block
    story.append(Paragraph("Einverständniserklärung", styles["title"]))
    story.append(Paragraph(
        "Bitte lesen Sie die folgenden Informationen sorgfältig durch, "
        "bevor Sie die Erklärung am Ende des Dokuments unterschreiben.",
        styles["subtitle"]))

    # ------------------------------------------------------------------ 1
    story += o.chapter("Allgemeine Informationen", styles)

    story += o.sub("Studienleitung",
        "Diese Studie wird am Institut für Medizinische Informatik, Biometrie "
        "und Epidemiologie (IMIBE) des Universitätsklinikums Essen "
        "durchgeführt.<br/><br/>"
        "Verantwortlicher Studienleiter: <b>Kilian Elfert, M.Sc.</b>", styles)

    story += o.sub("Ziel der Studie",
        "Ziel dieser Studie ist die Evaluation von KI-generierten klinischen "
        "Texten (Fallzusammenfassungen und Fragestellungen) für die "
        "Tumorboardanmeldung. Sie werden gebeten, eine Reihe von Fällen "
        "anhand festgelegter Kriterien zu bewerten.", styles)

    story += o.sub("Ablauf der Teilnahme",
        "Die Teilnahme erfolgt vollständig online. Sie beurteilen mehrere "
        "klinische Fallbeispiele anhand eines standardisierten "
        "Bewertungsbogens.", styles)

    story += o.sub("Freiwilligkeit und Widerruf",
        "Ihre Teilnahme ist freiwillig. Sie können die Studie jederzeit ohne "
        "Angabe von Gründen abbrechen. Bis zum Abschluss der Datenauswertung "
        "können Sie Ihre Einwilligung widerrufen; in diesem Fall werden alle "
        "bereits erhobenen Daten vollständig gelöscht.<br/><br/>"
        "Nach Abschluss der Auswertung oder einer möglichen Veröffentlichung "
        "können bereits anonymisierte und/oder verarbeitete Daten nicht mehr "
        "gelöscht werden.", styles)

    story += o.sub("Keine Auswirkungen auf die medizinische Versorgung",
        "Die Studie dient ausschließlich wissenschaftlichen Zwecken. Ihre "
        "Teilnahme hat keine Auswirkungen auf die laufende medizinische "
        "Versorgung.", styles)

    # ------------------------------------------------------------------ 2
    story += o.chapter("Datenschutz", styles)

    story += o.sub("Erhobene Daten", [
        "Ihre Bewertungen der Fallbeispiele",
        "Antwortzeiten und Interaktionsdaten innerhalb des Fragebogens",
        "Pseudonymisierte technische Metadaten (z.\u202fB. Zeitstempel)",
    ], styles)

    story += o.sub("Datenschutz und Datenverarbeitung",
        "Alle erhobenen Daten werden pseudonymisiert gespeichert und "
        "ausschließlich für wissenschaftliche Zwecke verwendet. Eine "
        "Weitergabe an Dritte erfolgt nicht. Die Verarbeitung erfolgt gemäß "
        "der Datenschutz-Grundverordnung (DSGVO) sowie den einschlägigen "
        "deutschen Datenschutzgesetzen.", styles)

    story += o.sub("Speicherdauer",
        "Die Daten werden für maximal 10 Jahre gespeichert, mindestens jedoch "
        "bis zur endgültigen Auswertung und Veröffentlichung der Ergebnisse. "
        "Danach werden sie gelöscht oder vollständig anonymisiert.", styles)

    story += o.sub("Trennung von Kontaktdaten und Studiendaten",
        "Ihre Kontaktdaten (z.\u202fB. E-Mail-Adresse) werden getrennt von "
        "den Bewertungsdaten gespeichert. Eine Zuordnung ist nur dem "
        "Forschungsteam möglich und erfolgt ausschließlich zur Verwaltung "
        "der Studienteilnahme.", styles)

    story += o.sub("Ihre Rechte nach DSGVO", [
        "Auskunft über die zu Ihrer Person gespeicherten Daten (Art.\u202f15 DSGVO)",
        "Berichtigung unrichtiger Daten (Art.\u202f16 DSGVO)",
        "Löschung Ihrer Daten (Art.\u202f17 DSGVO)",
        "Einschränkung der Verarbeitung (Art.\u202f18 DSGVO)",
        "Widerruf Ihrer Einwilligung (Art.\u202f7 DSGVO)",
        "Beschwerde bei einer Aufsichtsbehörde (Art.\u202f77 DSGVO)",
    ], styles)

    # ------------------------------------------------------------------ 3
    story += o.chapter("Kontakt", styles)

    story += o.sub("Studienleitung",
        "Bei inhaltlichen oder organisatorischen Fragen wenden Sie sich "
        "bitte an:<br/><b>Kilian Elfert, M.Sc.</b><br/>"
        "kilian.elfert@uk-essen.de", styles)

    story += o.sub("Datenschutzbeauftragter",
        "Universitätsklinikum Essen AöR<br/>"
        "Christian Hecke<br/>"
        "Hufelandstr.\u202f55, 45147 Essen<br/>"
        "E-Mail: datenschutz@uk-essen.de<br/>"
        "Telefon: +49\u202f(0)201\u202f723-6315", styles)

    # ------------------------------------------------------------------ 4
    story.append(PageBreak())
    story += o.chapter("Einverständniserklärung", styles)
    story.append(Spacer(1, 6 * mm))

    decl = [
        Paragraph(
            "Mit meiner Unterschrift bestätige ich,", styles["body"]),
        bullet_list([
            "dass ich mindestens 18 Jahre alt bin,",
            "dass ich die obenstehenden Informationen vollständig gelesen "
            "und verstanden habe,",
            "dass ich freiwillig an dieser Studie teilnehme,",
            "dass ich mit der Verarbeitung meiner Daten zu den genannten "
            "Zwecken einverstanden bin,",
            "dass ich über mein Widerrufsrecht informiert wurde.",
        ], styles),
        Spacer(1, 10 * mm),
        signature_block("Teilnehmer:in", styles, doc.width),
        Spacer(1, 8 * mm),
        signature_block("Studienleiter (Kilian Elfert, M.Sc.)",
                        styles, doc.width),
        Spacer(1, 4 * mm),
        Paragraph(
            "Hinweis: Diese Einwilligung wird in zweifacher Ausfertigung "
            "unterzeichnet \u2013 ein Exemplar verbleibt bei der "
            "teilnehmenden Person, das andere bei der Studienleitung.",
            styles["small"]),
    ]
    decl_table = Table([[decl]], colWidths=[doc.width])
    decl_table.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), SIG_BG),
        ("BOX",          (0, 0), (-1, -1), 0.6, SIG_TEAL),
        ("LEFTPADDING",  (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING",   (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
    ]))
    story.append(KeepTogether(decl_table))

    doc.build(story, canvasmaker=NumberedCanvas)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    here = Path(__file__).resolve().parent
    default_out = here / "exports" / "Einwilligung_Studienteilnahme_v2.pdf"

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--output", type=Path, default=default_out,
                    help=f"Output PDF path (default: {default_out}).")
    args = ap.parse_args()

    build_doc(args.output)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
