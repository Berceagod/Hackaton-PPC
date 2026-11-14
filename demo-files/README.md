# Demo files — FileGrid PPC

Material de upload manual în tab-ul **Încărcare**.  
`meter_readings` **nu** sunt aici — le generează simulatorul.

| Folder | Categorie UI | Storage politică | Ce conține |
|--------|----------------|------------------|------------|
| `outage_records/` | Înregistrări de avarie | S3, prioritate high | 3 PDF + 1 JSON evenimente Banat |
| `connection_docs/` | Documente de racordare | FTP + AES-256 | 4 PDF (PII **fictiv/redactat**) |
| `drone_videos/` | Video inspecții dronă | S3, chunked | raport PDF + JSON meta + 1 MP4 ~12 MB (3× chunk 5 MB) |

## Meta sugerate la upload

| Fișier | Zonă | Stație / linie |
|--------|------|----------------|
| `avarie_trafo_dobrogea_T12.pdf` | Dobrogea | ST-Constanta-Est |
| `raport_intrerupere_muntenia_LEA110.pdf` | Muntenia | ST-Bucuresti-Nord |
| `fisa_incident_ST-Timisoara-Vest.pdf` | Banat | ST-Timisoara-Vest |
| `evenimente_banat_2026-03-14.json` | Banat | Linie 20kV Timisoara |
| `cerere_racordare_banat_SC-Electro.pdf` | Banat | Linie 20kV Timisoara |
| `aviz_tehnic_muntenia_nr-4421.pdf` | Muntenia | LEA 20kV Ploiești |
| `acord_proprietar_teren_dobrogea.pdf` | Dobrogea | LEA 20kV |
| `inspectie_drona_muntenia_LEA20_small.mp4` | Muntenia | LEA 20kV Ploiești |

Clipurile voastre mari: puneți-le în `drone_videos/` ca `*_med.mp4` / `*_large.mp4`. MP4-urile sunt în `.gitignore` și `.dockerignore` — nu intră în imaginea Docker. PDF/JSON-urile rămân în repo.

Regenerare documente: `pip install fpdf2 pillow numpy imageio imageio-ffmpeg` apoi `python generate.py`.
