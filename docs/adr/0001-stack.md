# ADR-0001: Python FastAPI backend, keep React frontend
Status: accepted (supersedes the earlier Spring Boot decision; changed 2026-11-14 by the user's Python prompt)
Context: PDF §6 recommends Spring Boot/Angular but states any stack is allowed. The team already built the React 19 UI (4 tabs, chunked upload, integrity toast) and a Python simulator.
Decision: Backend = Python 3.11+, FastAPI + Uvicorn, in `backend/` (requirements.txt, Dockerfile python:3.11-slim). Frontend stays React; no migration to Angular.
Consequences: Same language as the simulator; zero rework of finished UI; deviation from the PDF recommendation is allowed and documented here.
