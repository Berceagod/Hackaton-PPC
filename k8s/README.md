# FileGrid pe Kubernetes (local, kind)

Aceeași aplicație ca în Docker Compose, pe un cluster Kubernetes local (kind = Kubernetes în Docker).
Rulează în paralel cu stack-ul Compose: Compose pe `https://localhost`, Kubernetes pe `https://localhost:8443`.

## Cerințe
- Docker Desktop pornit, `kubectl`, `kind` (`winget install Kubernetes.kind` pe Windows).
- `.env` creat de `./start.sh` (secretele pentru cluster se generează din el; nimic secret nu e în repo).

## Comenzi
```bash
k8s/up.sh       # creează clusterul, construiește și încarcă imaginile, secretele, aplică manifestele
k8s/verify.sh   # demo: expunere, upload/download, NetworkPolicy, simulator, scalare, autoscaling (HPA)
k8s/down.sh     # șterge clusterul (stack-ul Compose rămâne neatins)
```
`K8S_SKIP_LOAD=1 k8s/verify.sh` sare peste testul de autoscaling (cel mai lung pas, ~2-3 minute).

## Ce conține (`k8s/base`, aplicat cu `kubectl apply -k`)
| Componentă | Tip | Expus în afară? |
|---|---|---|
| nginx (TLS, UI, `/api/`) | Deployment + Service NodePort 30443 | da, singura intrare (`https://localhost:8443`) |
| backend | Deployment (2-6 pod-uri, HPA la 70% CPU) + Service | nu |
| postgres-meta, postgres-blob, minio, ftp | StatefulSet cu volum + Service | nu (NetworkPolicy: doar backend-ul) |
| simulator | Deployment | nu |

Decizii și limite: `docs/adr/0026-scale-out.md`, `docs/adr/0027-kubernetes.md`.
