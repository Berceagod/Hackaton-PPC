"""Generate credible FileGrid PPC demo documents (no real PII)."""

from __future__ import annotations

import json
from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parent
FONT = Path(r"C:\Windows\Fonts\arial.ttf")
FONT_B = Path(r"C:\Windows\Fonts\arialbd.ttf")
if not FONT.exists():
    FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    FONT_B = FONT


class Doc(FPDF):
    def __init__(self, subtitle: str):
        super().__init__(format="A4")
        self.subtitle = subtitle
        self.add_font("A", "", str(FONT))
        self.add_font("A", "B", str(FONT_B))
        self.set_auto_page_break(auto=True, margin=18)
        self.add_page()

    def header(self) -> None:
        self.set_fill_color(11, 18, 32)
        self.rect(0, 0, 210, 22, "F")
        self.set_text_color(34, 211, 238)
        self.set_font("A", "B", 11)
        self.set_xy(12, 6)
        self.cell(0, 6, "REȚELE ELECTRICE ROMÂNIA  ·  FileGrid PPC", new_x="LMARGIN", new_y="NEXT")
        self.set_font("A", "", 8)
        self.set_text_color(139, 155, 184)
        self.set_x(12)
        self.cell(0, 5, self.subtitle)
        self.set_text_color(20, 30, 48)
        self.ln(10)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("A", "", 8)
        self.set_text_color(120, 130, 148)
        self.cell(0, 8, f"Document demo — date fictive  |  pag. {self.page_no()}", align="C")

    def heading(self, text: str) -> None:
        self.set_x(12)
        self.set_font("A", "B", 14)
        self.set_text_color(11, 18, 32)
        self.multi_cell(186, 8, text)
        self.ln(2)

    def kv(self, key: str, value: str) -> None:
        y = self.get_y()
        self.set_font("A", "B", 9)
        self.set_text_color(80, 90, 110)
        self.set_xy(12, y)
        self.cell(50, 6, key)
        self.set_font("A", "", 9)
        self.set_text_color(20, 30, 48)
        self.set_xy(64, y)
        self.multi_cell(134, 6, value)
        self.set_x(12)

    def body(self, text: str) -> None:
        self.set_x(12)
        self.set_font("A", "", 10)
        self.set_text_color(30, 40, 55)
        self.multi_cell(186, 5.5, text)
        self.ln(2)

    def box(self, title: str, lines: list[str], fill=(245, 248, 252)) -> None:
        self.set_x(12)
        self.set_font("A", "B", 9)
        self.set_fill_color(*fill)
        self.cell(186, 7, title, new_x="LMARGIN", new_y="NEXT", fill=True)
        self.set_font("A", "", 9)
        for line in lines:
            self.set_x(12)
            self.multi_cell(186, 5, line)
        self.ln(2)


def save(pdf: Doc, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    print(f"  {path.relative_to(ROOT)}  ({path.stat().st_size:,} B)")


def outage_trafo() -> None:
    p = Doc("Înregistrare de avarie  ·  stație de transformare")
    p.heading("Raport avarie — ST Constanța Est / Trafo T12")
    p.kv("Cod incident", "INC-DOB-2026-0314-T12")
    p.kv("Zonă", "Dobrogea")
    p.kv("Stație / echipament", "ST-Constanta-Est · Trafo 110/20 kV T12")
    p.kv("Linie afectată", "LEA 110 kV Constanța–Medgidia")
    p.kv("Clasă prioritate", "HIGH — dispecerat")
    p.kv("Declanșare", "2026-03-14 18:42:11 EET")
    p.kv("Remediere (estimat)", "2026-03-14 21:15 EET")
    p.kv("Consumatori afectați", "~12.400 (JT), 3 PT-uri rurale")
    p.body(
        "În timpul furtunii din 14 martie, protecțiile T12 au declanșat pe maximală de curent "
        "după un defect pe LEA 110 kV. Dispeceratul a izolat bara 20 kV și a transferat "
        "sarcina pe T11. Nu s-au înregistrat victime. S-a dispus inspecție vizuală a izolatoarelor "
        "și verificare ulei transformator."
    )
    p.box(
        "Măsuri operative",
        [
            "1. Confirmare SCADA: I>, 52-T12 OPEN, 89-B20 OPEN.",
            "2. Echipă intervenție Constanța — ETA 45 min.",
            "3. Comunicat clienți (SMS/app) — întrerupere planificată restabilire 21:00.",
            "4. După revenire: citire gaze dizolvate + termoviziune.",
        ],
        fill=(255, 246, 230),
    )
    p.kv("Întocmit", "Dispecer Muntenia / FileGrid  ·  dispecer@ppc.ro")
    save(p, ROOT / "outage_records" / "avarie_trafo_dobrogea_T12.pdf")


def outage_lea() -> None:
    p = Doc("Înregistrare de avarie  ·  LEA 110 kV")
    p.heading("Raport întrerupere — LEA 110 kV Ploiești Nord")
    p.kv("Cod incident", "INC-MUN-2026-0314-LEA110")
    p.kv("Zonă", "Muntenia")
    p.kv("Stație / linie", "ST-Bucuresti-Nord · LEA 110 kV Ploiești Nord")
    p.kv("Cauză probabilă", "Cădere vegetație pe conductor (furtună)")
    p.kv("Durată întrerupere", "00:38 h")
    p.kv("Reanclanșare automată", "RAR 1 eșuat · RAR 2 reușit după izolare tronson")
    p.body(
        "Sistemul de protecție a izolat tronsonul 14–17. Echipa de teren a confirmat un arbore "
        "pe faza R în zona coridorului km 22.4. Tronsonul a fost retras sub tensiune după "
        "curățare. Se solicită inspecție dronă a coridorului (vezi categorie drone_videos)."
    )
    p.box(
        "Impact rețea",
        [
            "Sarcină transferată: 38 MW pe LEA paralelă.",
            "Fără cascadă pe 220 kV. Frecvență sistem 49.97–50.04 Hz.",
            "PT-uri afectate: MUN-PT-004, MUN-PT-007, MUN-PT-011.",
        ],
    )
    save(p, ROOT / "outage_records" / "raport_intrerupere_muntenia_LEA110.pdf")


def outage_timisoara() -> None:
    p = Doc("Fișă incident  ·  stație 110/20 kV")
    p.heading("Fișă incident — ST Timișoara Vest")
    p.kv("Cod incident", "INC-BAN-2026-0314-TV")
    p.kv("Zonă", "Banat")
    p.kv("Stație", "ST-Timisoara-Vest")
    p.kv("Echipament", "Întrerupător 20 kV celula 7 (ieșire LEA 20 kV Giroc)")
    p.kv("Tip eveniment", "Declanșare pe defect fugitiv — revenire OK")
    p.body(
        "Declanșare unică la 07:12, fără reanclanșare eșuată. Termoviziune pe contacte: normal. "
        "Se menține supraveghere 24 h. Nu se clasifică avarie majoră; documentul rămâne în "
        "registrul de evenimente pentru corelare cu citirile de contoare din zonă."
    )
    save(p, ROOT / "outage_records" / "fisa_incident_ST-Timisoara-Vest.pdf")


def outage_json() -> None:
    data = {
        "source": "FileGrid demo",
        "zone": "Banat",
        "generated_for": "outage_records",
        "date": "2026-03-14",
        "events": [
            {
                "event_id": "EVT-BAN-001",
                "ts": "2026-03-14T06:11:02+02:00",
                "station": "ST-Timisoara-Vest",
                "asset": "celula-20kV-07",
                "type": "trip",
                "severity": "low",
                "consumers_affected": 820,
                "duration_min": 4,
            },
            {
                "event_id": "EVT-BAN-002",
                "ts": "2026-03-14T18:44:19+02:00",
                "station": "Linie 20kV Timisoara",
                "asset": "LEA-20-Giroc",
                "type": "voltage_sag",
                "severity": "medium",
                "note": "Corelare cu anomalii contoare (voltage_sag) din simulator",
            },
        ],
    }
    path = ROOT / "outage_records" / "evenimente_banat_2026-03-14.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {path.relative_to(ROOT)}  ({path.stat().st_size:,} B)")


def cerere_racordare() -> None:
    p = Doc("Document de racordare  ·  CONFIDENȚIAL  ·  AES-256-GCM la stocare")
    p.heading("Cerere de racordare la rețeaua de distribuție")
    p.kv("Nr. înregistrare", "CR-BAN-2026-0881")
    p.kv("Zonă", "Banat")
    p.kv("Solicitant (demo)", "SC Electro Banat SRL  ·  CUI RO 99887766")
    p.kv("Reprezentant", "Popescu Andrei (date fictive)")
    p.kv("CI (redactat)", "KX ******  ·  CNP ***********")
    p.kv("Adresă punct de consum", "Timișoara, str. Demo 14, jud. Timiș")
    p.kv("Putere solicitată", "55 kW  ·  3×400 V  ·  branșament subteran")
    p.kv("Linie / PT", "Linie 20kV Timisoara · PT BAN-PT-003")
    p.body(
        "Solicităm racordarea instalației de utilizare conform Avizului Tehnic de Racordare. "
        "Anexe: copie CI (redactată), extras CF, plan de situație. Documentul conține date cu "
        "caracter personal — în FileGrid se stochează criptat (AES-256-GCM) pe FTP, cu audit log."
    )
    p.box(
        "Declarație",
        [
            "Subsemnatul declar că datele sunt corecte. Document DEMO — nu conține PII reale.",
            "Semnătură: [semnătură electronică mock]  ·  2026-02-20",
        ],
        fill=(237, 233, 254),
    )
    save(p, ROOT / "connection_docs" / "cerere_racordare_banat_SC-Electro.pdf")


def copie_ci() -> None:
    p = Doc("Document de racordare  ·  copie act identitate  ·  REDACTAT")
    p.heading("Copie carte de identitate — specimen demo")
    p.kv("Nume", "POPESCU")
    p.kv("Prenume", "ANDREI")
    p.kv("CNP", "1*********** (redactat)")
    p.kv("Serie / nr.", "KX · ******")
    p.kv("Domiciliu", "Timișoara, [adresă fictivă]")
    p.kv("Valabilitate", "2024–2034")
    p.body(
        "Acest specimen înlocuiește o copie CI reală. Câmpurile sunt mascate intenționat. "
        "Scop: demonstrație criptare + audit la descărcare în FileGrid (categorie connection_docs)."
    )
    p.box("WATERMARK", ["SPECIMEN  ·  NU ESTE UN ACT REAL  ·  FILEGRID DEMO"], fill=(255, 240, 240))
    save(p, ROOT / "connection_docs" / "copie_CI_redactata_demo.pdf")


def aviz_tehnic() -> None:
    p = Doc("Document de racordare  ·  Aviz tehnic de racordare")
    p.heading("Aviz Tehnic de Racordare nr. 4421 / 2026")
    p.kv("Zonă", "Muntenia")
    p.kv("Emitent", "Rețele Electrice România — Sucursala Muntenia Nord")
    p.kv("Beneficiar (demo)", "SC Lumina Urban SRL")
    p.kv("Punct de racordare", "PT MUN-PT-008  ·  LEA 20 kV Ploiești")
    p.kv("Putere aprobată", "120 kW")
    p.kv("Punct de măsură", "Contor inteligent trifazat — serie SIM-DEMO-4421")
    p.body(
        "Se avizează racordarea în condițiile respectării soluției tehnice: branșament 20 kV, "
        "celulă măsură, SEP. Valabil 12 luni de la emitere. Anexă: schema monofilară (nespecificată "
        "în acest demo)."
    )
    p.kv("Întocmit", "Serviciu racordări Muntenia  ·  2026-01-15")
    save(p, ROOT / "connection_docs" / "aviz_tehnic_muntenia_nr-4421.pdf")


def acord_proprietar() -> None:
    p = Doc("Document de racordare  ·  acord proprietar teren")
    p.heading("Acord proprietar pentru ocupare coridor LEA")
    p.kv("Zonă", "Dobrogea")
    p.kv("Proprietar (demo)", "Ionescu Maria  ·  CF 102938-Dobrogea")
    p.kv("Imobil", "Teren extravilan, tarla 12, parcela 4, com. Demo")
    p.kv("Obiectiv", "Stâlp LEA 20 kV nr. 88–89 + servitute de trecere 4 m")
    p.body(
        "Proprietarul își exprimă acordul pentru amplasarea infrastructurii de distribuție, "
        "cu despăgubire conform evaluării anexate (nespecificată). Document DEMO, date fictive."
    )
    save(p, ROOT / "connection_docs" / "acord_proprietar_teren_dobrogea.pdf")


def drone_report() -> None:
    p = Doc("Inspecție dronă  ·  coridor LEA 20 kV")
    p.heading("Raport inspecție aeriană — LEA 20 kV Ploiești")
    p.kv("Zonă", "Muntenia")
    p.kv("Linie", "LEA 20kV Ploiești")
    p.kv("Zbor", "2026-03-12  ·  operator DRN-04  ·  firmware 2.4.1")
    p.kv("Tronson", "stâlpi 40–72  ·  6.8 km")
    p.kv("Fișier video asociat", "inspectie_drona_muntenia_LEA20_small.mp4")
    p.body(
        "S-au identificat 3 zone cu vegetație în gabarit (stâlpii 52, 61, 68) și un izolator "
        "cu urmă de conturnare (stâlp 47). Recomandare: tăiere vegetație + inspecție terestră "
        "izolator. Clipul video este materialul brut; acest PDF e livrabilul de dispecerat."
    )
    p.box(
        "Findings",
        [
            "VEG-HIGH  stâlp 52, 61, 68 — distanță conductor–coroană < 1.5 m",
            "INSULATOR  stâlp 47 — urmă conturnare fază S",
            "OK restul tronsonului",
        ],
        fill=(224, 247, 250),
    )
    save(p, ROOT / "drone_videos" / "raport_inspectie_muntenia_LEA20.pdf")


def drone_meta() -> None:
    meta = {
        "filename": "inspectie_drona_muntenia_LEA20_small.mp4",
        "category": "drone_videos",
        "zone": "Muntenia",
        "station": "LEA 20kV Ploiești",
        "description": "Inspecție coridor vegetație, stâlpi 40–72",
        "note": "Clip scurt generat pentru demo chunked upload (>5 MB dacă encoder-ul e disponibil). "
        "Puneți aici și MP4-urile voastre mari (*_med / *_large) — sunt ignorate de git/Docker.",
    }
    path = ROOT / "drone_videos" / "inspectie_drona_muntenia_LEA20_meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {path.relative_to(ROOT)}  ({path.stat().st_size:,} B)")


def try_small_video() -> None:
    """Synthetic 720p clip ~8 MB so FileGrid uses chunked upload (>5 MB)."""
    out = ROOT / "drone_videos" / "inspectie_drona_muntenia_LEA20_small.mp4"
    try:
        import subprocess

        import imageio_ffmpeg
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        print(f"  [skip video] lipsă pachet: {exc}")
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    font_path = str(FONT)
    try:
        font = ImageFont.truetype(font_path, 32)
        font_sm = ImageFont.truetype(font_path, 20)
    except OSError:
        font = ImageFont.load_default()
        font_sm = font

    fps, seconds, w, h = 24, 10, 1280, 720
    n = fps * seconds
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{w}x{h}",
        "-r", str(fps),
        "-i", "-",
        "-an",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-b:v", "6.5M",
        "-preset", "veryfast",
        str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert proc.stdin is not None
    rng = np.random.default_rng(42)
    try:
        for i in range(n):
            img = Image.new("RGB", (w, h), (11, 18, 32))
            d = ImageDraw.Draw(img)
            y = 260 + int(50 * np.sin(i / 8))
            d.line([(0, y), (w, y - 40)], fill=(245, 158, 11), width=6)
            d.line([(0, y + 90), (w, y + 50)], fill=(245, 158, 11), width=6)
            for x in range(80, w, 180):
                d.rectangle([x, 140, x + 14, 430], fill=(139, 155, 184))
            d.rectangle([0, 0, w, 64], fill=(18, 35, 56))
            d.text(
                (20, 14),
                "FileGrid PPC  ·  inspecție dronă  ·  Muntenia  ·  LEA 20 kV",
                font=font,
                fill=(34, 211, 238),
            )
            d.text(
                (20, h - 40),
                f"stâlp {40 + i // 8}   frame {i + 1}/{n}   DEMO (nu e footage real)",
                font=font_sm,
                fill=(139, 155, 184),
            )
            arr = np.array(img, dtype=np.int16)
            arr += rng.integers(-28, 29, size=arr.shape, dtype=np.int16)
            proc.stdin.write(np.clip(arr, 0, 255).astype(np.uint8).tobytes())
    finally:
        proc.stdin.close()
        proc.wait()
    print(f"  {out.relative_to(ROOT)}  ({out.stat().st_size:,} B)")


def main() -> None:
    print("Generating demo files…")
    outage_trafo()
    outage_lea()
    outage_timisoara()
    outage_json()
    cerere_racordare()
    copie_ci()
    aviz_tehnic()
    acord_proprietar()
    drone_report()
    drone_meta()
    try_small_video()
    print("Done.")


if __name__ == "__main__":
    main()
