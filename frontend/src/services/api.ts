import type {
  AnalyticsSummary,
  AuditEntry,
  AuthSession,
  AuthUser,
  CategoryPolicy,
  ChunkUploadResponse,
  DownloadResult,
  FileCategory,
  FileMetadata,
  FileRecord,
  IngestResponse,
  PoliciesMap,
  StorageBackend,
  UploadProgress,
} from '../types/filegrid'
import {
  CHUNK_SIZE,
  CHUNK_THRESHOLD,
  ZONE_OPTIONS,
} from '../types/filegrid'

const API_URL = import.meta.env.VITE_API_URL || '/api/v1'
export const USE_MOCK = import.meta.env.VITE_USE_MOCK === 'true'

const LS_TOKEN = 'filegrid_token'
const LS_USER = 'filegrid_user'
const LS_FILES = 'filegrid_files'
const LS_POLICIES = 'filegrid_policies'
const LS_AUDIT = 'filegrid_audit'
/** In-memory chunk assembly (avoids localStorage quota for large drone videos) */
const chunkSessions = new Map<
  string,
  {
    received: Set<number>
    total: number
    meta: Partial<FileRecord> & { firstChunkB64?: string }
  }
>()

const MOCK_USERS: Record<string, { password: string; user: AuthUser }> = {
  'dispecer@ppc.ro': {
    password: 'dispecer',
    user: { email: 'dispecer@ppc.ro', role: 'dispecer', name: 'Dispecer Muntenia' },
  },
  'admin@ppc.ro': {
    password: 'admin',
    user: { email: 'admin@ppc.ro', role: 'admin', name: 'Admin FileGrid' },
  },
}

const DEFAULT_POLICIES: PoliciesMap = {
  meter_readings: {
    storage: 'postgres',
    encryption: false,
    deduplicate: true,
    priority: 'normal',
  },
  outage_records: {
    storage: 's3',
    encryption: false,
    deduplicate: false,
    priority: 'high',
  },
  connection_docs: {
    storage: 'ftp',
    encryption: true,
    deduplicate: false,
    priority: 'normal',
  },
  drone_videos: {
    storage: 's3',
    encryption: false,
    deduplicate: false,
    priority: 'low',
  },
}

function uuid(): string {
  return crypto.randomUUID()
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}


function utf8ToBase64(str: string): string {
  const bytes = new TextEncoder().encode(str)
  let binary = ''
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]!)
  return btoa(binary)
}

export async function computeSha256(data: ArrayBuffer | Blob): Promise<string> {
  const buffer = data instanceof Blob ? await data.arrayBuffer() : data
  const hashBuffer = await crypto.subtle.digest('SHA-256', buffer)
  return Array.from(new Uint8Array(hashBuffer))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = reader.result as string
      resolve(result.split(',')[1] ?? '')
    }
    reader.onerror = reject
    reader.readAsDataURL(blob)
  })
}

function base64ToBlob(b64: string, mime = 'application/octet-stream'): Blob {
  const binary = atob(b64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return new Blob([bytes], { type: mime })
}

function getToken(): string | null {
  return localStorage.getItem(LS_TOKEN)
}

export function getStoredSession(): AuthSession | null {
  const token = localStorage.getItem(LS_TOKEN)
  const userRaw = localStorage.getItem(LS_USER)
  if (!token || !userRaw) return null
  try {
    return { token, user: JSON.parse(userRaw) as AuthUser }
  } catch {
    return null
  }
}

export function clearSession(): void {
  localStorage.removeItem(LS_TOKEN)
  localStorage.removeItem(LS_USER)
}

function loadFiles(): FileRecord[] {
  try {
    return JSON.parse(localStorage.getItem(LS_FILES) || '[]') as FileRecord[]
  } catch {
    return []
  }
}

function saveFiles(files: FileRecord[]): void {
  localStorage.setItem(LS_FILES, JSON.stringify(files))
}

function loadPolicies(): PoliciesMap {
  try {
    const raw = localStorage.getItem(LS_POLICIES)
    if (!raw) return { ...DEFAULT_POLICIES }
    return { ...DEFAULT_POLICIES, ...JSON.parse(raw) } as PoliciesMap
  } catch {
    return { ...DEFAULT_POLICIES }
  }
}

function savePolicies(policies: PoliciesMap): void {
  localStorage.setItem(LS_POLICIES, JSON.stringify(policies))
}

function loadAudit(): AuditEntry[] {
  try {
    return JSON.parse(localStorage.getItem(LS_AUDIT) || '[]') as AuditEntry[]
  } catch {
    return []
  }
}

function saveAudit(entries: AuditEntry[]): void {
  localStorage.setItem(LS_AUDIT, JSON.stringify(entries))
}

function addAudit(
  fileId: string,
  action: 'upload' | 'download',
  details?: string,
): void {
  const session = getStoredSession()
  const entries = loadAudit()
  entries.unshift({
    id: uuid(),
    file_id: fileId,
    action,
    user: session?.user.email ?? 'system',
    timestamp: new Date().toISOString(),
    details,
  })
  saveAudit(entries.slice(0, 500))
}

function seedDemoFiles(): void {
  if (loadFiles().length > 0) return
  const now = Date.now()
  const demo: FileRecord[] = [
    {
      id: uuid(),
      filename: 'citiri_muntenia_2026-03-14.csv',
      category: 'meter_readings',
      storage: 'postgres',
      size_bytes: 24576,
      sha256: 'a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456',
      created_at: new Date(now - 3600_000).toISOString(),
      encrypted: false,
      trace_id: uuid(),
      zone: 'Prahova',
      station: 'ST-Bucuresti-Nord',
      uploaded_by: 'simulator@ppc.ro',
      content_b64: utf8ToBase64(
        'meter_id,voltage,current\nM001,230.1,12.4\nM002,229.8,11.9\n',
      ),
    },
    {
      id: uuid(),
      filename: 'avarie_trafo_dobrogea_T12.pdf',
      category: 'outage_records',
      storage: 's3',
      size_bytes: 1_048_576,
      sha256: 'b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef12345678',
      created_at: new Date(now - 7200_000).toISOString(),
      encrypted: false,
      trace_id: uuid(),
      zone: 'Constanța',
      station: 'ST-Constanta-Est',
      description: 'Întrerupere LEA 110kV — furtună',
      uploaded_by: 'dispecer@ppc.ro',
      content_b64: utf8ToBase64('Raport avarie ST-Constanta-Est — LEA 110kV'),
    },
    {
      id: uuid(),
      filename: 'cerere_racordare_banat_SC-Electro.pdf',
      category: 'connection_docs',
      storage: 'ftp',
      size_bytes: 512_000,
      sha256: 'c3d4e5f6789012345678901234567890abcdef1234567890abcdef1234567890',
      created_at: new Date(now - 86400_000).toISOString(),
      encrypted: true,
      trace_id: uuid(),
      zone: 'Timiș',
      station: 'Linie 20kV Timisoara',
      uploaded_by: 'admin@ppc.ro',
      content_b64: utf8ToBase64('Document racordare criptat AES-256-GCM'),
    },
    {
      id: uuid(),
      filename: 'inspectie_drona_muntenia_LEA20.mp4',
      category: 'drone_videos',
      storage: 's3',
      size_bytes: 157_286_400,
      sha256: 'd4e5f6789012345678901234567890abcdef1234567890abcdef1234567890ab',
      created_at: new Date(now - 172800_000).toISOString(),
      encrypted: false,
      trace_id: uuid(),
      zone: 'Prahova',
      station: 'LEA 20kV Ploiești',
      description: 'Inspecție coridor vegetație',
      uploaded_by: 'dispecer@ppc.ro',
      content_b64: utf8ToBase64('mock-drone-video-content'),
    },
  ]
  saveFiles(demo)
  for (const f of demo) {
    addAudit(f.id, 'upload', `Seed demo: ${f.filename}`)
  }
}

if (USE_MOCK) {
  try {
    seedDemoFiles()
    if (!localStorage.getItem(LS_POLICIES)) savePolicies(DEFAULT_POLICIES)
  } catch (err) {
    console.error('FileGrid mock seed failed:', err)
  }
}

async function apiFetch(
  path: string,
  options: RequestInit = {},
): Promise<Response> {
  const token = getToken()
  const headers = new Headers(options.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (!headers.has('X-Trace-Id')) headers.set('X-Trace-Id', uuid())

  const res = await fetch(`${API_URL}${path}`, { ...options, headers })
  // Expired/invalid token (e.g. secrets regenerated by start.sh): back to the login screen
  // instead of every tab failing with 401 toasts.
  if (res.status === 401 && path !== '/auth/login' && token) {
    clearSession()
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT))
  }
  return res
}

export const SESSION_EXPIRED_EVENT = 'filegrid:session-expired'

export type ApiHealth = 'ok' | 'degraded' | 'offline'

/** Backend + storage status for the header badge. */
export async function getHealth(): Promise<ApiHealth> {
  if (USE_MOCK) return 'ok'
  try {
    const res = await apiFetch('/health')
    if (!res.ok) return 'offline'
    const body = (await res.json()) as { status?: string }
    return body.status === 'ok' ? 'ok' : 'degraded'
  } catch {
    return 'offline'
  }
}

/** Prefer the exact UTF-8 name (RFC 6266 `filename*`), fall back to `filename`. */
function filenameFromDisposition(disposition: string | null): string | null {
  if (!disposition) return null
  const star = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (star) {
    try {
      return decodeURIComponent(star[1])
    } catch {
      // malformed encoding: use the ASCII name below
    }
  }
  return disposition.match(/filename="?([^";]+)"?/i)?.[1] ?? null
}

// ─── Auth ───────────────────────────────────────────────────────────────────

export async function login(email: string, password: string): Promise<AuthSession> {
  if (USE_MOCK) {
    await delay(400)
    const entry = MOCK_USERS[email.toLowerCase()]
    if (!entry || entry.password !== password) {
      throw new Error('Credențiale invalide. Folosiți dispecer@ppc.ro / dispecer sau admin@ppc.ro / admin')
    }
    const token = `mock-jwt.${btoa(JSON.stringify({ sub: email, exp: Date.now() + 86400_000 }))}`
    const session: AuthSession = { token, user: entry.user }
    localStorage.setItem(LS_TOKEN, token)
    localStorage.setItem(LS_USER, JSON.stringify(entry.user))
    return session
  }

  const res = await apiFetch('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!res.ok) throw new Error('Autentificare eșuată')
  const data = (await res.json()) as AuthSession
  localStorage.setItem(LS_TOKEN, data.token)
  localStorage.setItem(LS_USER, JSON.stringify(data.user))
  return data
}

export function logout(): void {
  clearSession()
}

// ─── Ingest / Chunked Upload ────────────────────────────────────────────────

export async function ingestFile(
  file: File,
  category: FileCategory,
  metadata: FileMetadata = {},
): Promise<IngestResponse> {
  if (USE_MOCK) {
    await delay(300 + Math.random() * 400)
    const policies = loadPolicies()
    const policy = policies[category]
    const sha256 = await computeSha256(file)
    const files = loadFiles()

    if (policy.deduplicate) {
      const existing = files.find((f) => f.sha256 === sha256 && f.category === category)
      if (existing) {
        return {
          file_id: existing.id,
          trace_id: existing.trace_id,
          sha256: existing.sha256,
          storage: existing.storage,
        }
      }
    }

    const content_b64 =
      file.size <= 2 * 1024 * 1024
        ? await blobToBase64(file)
        : utf8ToBase64(`mock:${file.name}:${sha256}`)

    const record: FileRecord = {
      id: uuid(),
      filename: file.name,
      category,
      storage: policy.storage,
      size_bytes: file.size,
      sha256,
      created_at: new Date().toISOString(),
      encrypted: policy.encryption,
      trace_id: uuid(),
      zone: metadata.zone as string | undefined,
      station: metadata.station,
      description: metadata.description,
      uploaded_by: getStoredSession()?.user.email,
      content_b64,
    }
    files.unshift(record)
    saveFiles(files)
    addAudit(record.id, 'upload', `Ingest ${category} → ${policy.storage}`)
    return {
      file_id: record.id,
      trace_id: record.trace_id,
      sha256: record.sha256,
      storage: record.storage,
    }
  }

  const form = new FormData()
  form.append('file', file)
  form.append('category', category)
  form.append('metadata', JSON.stringify(metadata))
  const res = await apiFetch('/ingest', { method: 'POST', body: form })
  if (!res.ok) throw new Error(`Ingest eșuat: ${res.status}`)
  return (await res.json()) as IngestResponse
}

export async function uploadChunk(
  uploadId: string,
  chunkIndex: number,
  totalChunks: number,
  chunk: Blob,
  meta?: {
    filename: string
    category: FileCategory
    metadata: FileMetadata
    sha256?: string
    size_bytes?: number
  },
): Promise<ChunkUploadResponse> {
  if (USE_MOCK) {
    // Simulate network instability (~12% failure for demo retry)
    if (Math.random() < 0.12) {
      await delay(200)
      throw new Error('Network timeout — chunk upload failed')
    }
    await delay(80 + Math.random() * 120)

    let session = chunkSessions.get(uploadId)
    if (!session) {
      session = {
        received: new Set(),
        total: totalChunks,
        meta: {
          filename: meta?.filename,
          category: meta?.category,
          zone: meta?.metadata.zone as string | undefined,
          station: meta?.metadata.station,
          description: meta?.metadata.description,
          size_bytes: meta?.size_bytes,
          sha256: meta?.sha256,
        },
      }
      chunkSessions.set(uploadId, session)
    }
    if (chunkIndex === 0 && !session.meta.firstChunkB64) {
      session.meta.firstChunkB64 = await blobToBase64(chunk)
    }
    session.received.add(chunkIndex)
    const received = session.received.size

    if (received >= totalChunks) {
      const policies = loadPolicies()
      const category = (session.meta.category as FileCategory) || 'drone_videos'
      const policy = policies[category]
      const sha256 = session.meta.sha256 || (await computeSha256(chunk))
      const files = loadFiles()
      const record: FileRecord = {
        id: uuid(),
        filename: session.meta.filename || `upload-${uploadId}`,
        category,
        storage: policy.storage,
        size_bytes: session.meta.size_bytes || 0,
        sha256,
        created_at: new Date().toISOString(),
        encrypted: policy.encryption,
        trace_id: uuid(),
        zone: session.meta.zone,
        station: session.meta.station,
        description: session.meta.description,
        uploaded_by: getStoredSession()?.user.email,
        content_b64: session.meta.firstChunkB64 ?? utf8ToBase64(`mock-chunked:${sha256}`),
      }
      files.unshift(record)
      saveFiles(files)
      addAudit(record.id, 'upload', `Chunked upload (${totalChunks} chunks) → ${policy.storage}`)
      chunkSessions.delete(uploadId)
        ; (uploadChunk as unknown as { _lastResult?: IngestResponse })._lastResult = {
          file_id: record.id,
          trace_id: record.trace_id,
          sha256: record.sha256,
          storage: record.storage,
        }
    }

    return { upload_id: uploadId, received_chunks: received }
  }

  const form = new FormData()
  form.append('upload_id', uploadId)
  form.append('chunk_index', String(chunkIndex))
  form.append('total_chunks', String(totalChunks))
  // The backend needs to know which file this is (first chunk to arrive opens the upload)
  form.append('filename', meta?.filename ?? 'file')
  form.append('category', meta?.category ?? 'drone_videos')
  form.append('metadata', JSON.stringify(meta?.metadata ?? {}))
  if (meta?.sha256) form.append('sha256', meta.sha256)
  if (meta?.size_bytes !== undefined) form.append('size_bytes', String(meta.size_bytes))
  // Per-chunk checksum: a chunk damaged in transit is rejected (422) and resent
  form.append('chunk_sha256', await computeSha256(chunk))
  form.append('file_chunk', chunk)
  const res = await apiFetch('/upload/chunk', { method: 'POST', body: form })
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as
      | { detail?: { error?: string; message?: string } | string }
      | null
    const detail = typeof body?.detail === 'object' ? body.detail.message : body?.detail
    const err = new Error(`Chunk upload eșuat: ${res.status}${detail ? ` — ${detail}` : ''}`) as
      Error & { status: number }
    err.status = res.status
    throw err
  }
  return (await res.json()) as ChunkUploadResponse
}

const CHUNK_RETRIES = 6 // ~16 s of back-off per chunk on an unstable link
const FINAL_CHUNK_STATUSES = [401, 403, 404, 409, 413, 501]
// crypto.subtle has no streaming hash: above this size the whole-file SHA-256 would need the
// entire file in browser memory. The server still hashes it, and every chunk is verified.
const WHOLE_FILE_HASH_LIMIT = 512 * 1024 * 1024

async function uploadChunkWithRetry(
  uploadId: string,
  chunkIndex: number,
  totalChunks: number,
  chunk: Blob,
  meta: {
    filename: string
    category: FileCategory
    metadata: FileMetadata
    sha256?: string
    size_bytes?: number
  },
  onRetry?: (attempt: number) => void,
): Promise<ChunkUploadResponse> {
  let lastError: Error | null = null
  for (let attempt = 1; attempt <= CHUNK_RETRIES; attempt++) {
    try {
      return await uploadChunk(uploadId, chunkIndex, totalChunks, chunk, meta)
    } catch (err) {
      lastError = err instanceof Error ? err : new Error(String(err))
      const status = (err as { status?: number }).status
      if (status !== undefined && FINAL_CHUNK_STATUSES.includes(status)) break
      if (attempt < CHUNK_RETRIES) {
        onRetry?.(attempt)
        await delay(Math.min(500 * Math.pow(2, attempt - 1), 8000))
      }
    }
  }
  throw lastError ?? new Error('Chunk upload failed after retries')
}

export async function uploadFileResilient(
  file: File,
  category: FileCategory,
  metadata: FileMetadata = {},
  onProgress?: (progress: UploadProgress) => void,
): Promise<IngestResponse> {
  onProgress?.({
    percent: 0,
    currentChunk: 0,
    totalChunks: 1,
    speedMBps: 0,
    status: 'hashing',
    message: 'Calcul SHA-256…',
  })

  const sha256 =
    file.size <= WHOLE_FILE_HASH_LIMIT || USE_MOCK ? await computeSha256(file) : undefined
  const useChunked = file.size > CHUNK_THRESHOLD || category === 'drone_videos'

  if (!useChunked) {
    onProgress?.({
      percent: 30,
      currentChunk: 1,
      totalChunks: 1,
      speedMBps: 0,
      status: 'uploading',
      message: 'Trimitere către /ingest…',
    })
    const start = performance.now()
    const result = await ingestFile(file, category, metadata)
    const elapsed = (performance.now() - start) / 1000
    const speed = elapsed > 0 ? file.size / (1024 * 1024) / elapsed : 0
    onProgress?.({
      percent: 100,
      currentChunk: 1,
      totalChunks: 1,
      speedMBps: speed,
      status: 'complete',
      message: 'Upload finalizat',
    })
    return { ...result, sha256: result.sha256 || sha256 || '' }
  }

  const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE))
  const uploadId = uuid()
  const start = performance.now()
  let uploadedBytes = 0
  let lastResponse: ChunkUploadResponse | null = null

  for (let i = 0; i < totalChunks; i++) {
    const startByte = i * CHUNK_SIZE
    const endByte = Math.min(startByte + CHUNK_SIZE, file.size)
    const chunk = file.slice(startByte, endByte)

    onProgress?.({
      percent: Math.round((i / totalChunks) * 100),
      currentChunk: i + 1,
      totalChunks,
      speedMBps: uploadedBytes / (1024 * 1024) / Math.max((performance.now() - start) / 1000, 0.001),
      status: 'uploading',
      message: `Trimitere chunk ${i + 1}/${totalChunks}`,
    })

    lastResponse = await uploadChunkWithRetry(
      uploadId,
      i,
      totalChunks,
      chunk,
      {
        filename: file.name,
        category,
        metadata,
        sha256,
        size_bytes: file.size,
      },
      (attempt) => {
        onProgress?.({
          percent: Math.round((i / totalChunks) * 100),
          currentChunk: i + 1,
          totalChunks,
          speedMBps: uploadedBytes / (1024 * 1024) / Math.max((performance.now() - start) / 1000, 0.001),
          status: 'retrying',
          retryAttempt: attempt,
          message: `Retry chunk ${i + 1} (încercare ${attempt}/${CHUNK_RETRIES - 1})…`,
        })
      },
    )

    uploadedBytes += chunk.size
    const elapsed = Math.max((performance.now() - start) / 1000, 0.001)
    onProgress?.({
      percent: Math.round(((i + 1) / totalChunks) * 100),
      currentChunk: i + 1,
      totalChunks,
      speedMBps: uploadedBytes / (1024 * 1024) / elapsed,
      status: i + 1 === totalChunks ? 'complete' : 'uploading',
      message: i + 1 === totalChunks ? 'Upload finalizat' : undefined,
    })
  }

  if (USE_MOCK) {
    const last = (uploadChunk as unknown as { _lastResult?: IngestResponse })._lastResult
    if (last) return last
    // Fallback: find by sha256
    const found = loadFiles().find((f) => f.sha256 === sha256)
    if (found) {
      return {
        file_id: found.id,
        trace_id: found.trace_id,
        sha256: found.sha256,
        storage: found.storage,
      }
    }
  }

  if (lastResponse?.status === 'complete' && lastResponse.file_id) {
    return {
      file_id: lastResponse.file_id,
      trace_id: lastResponse.trace_id ?? '',
      sha256: lastResponse.sha256 ?? sha256 ?? '',
      storage: lastResponse.storage ?? loadPolicies()[category].storage,
    }
  }
  if (!USE_MOCK) throw new Error('Upload incomplet: serverul nu a confirmat fișierul')
  return {
    file_id: uploadId,
    trace_id: uuid(),
    sha256: sha256 ?? '',
    storage: loadPolicies()[category].storage,
  }
}

// ─── Files ──────────────────────────────────────────────────────────────────

export interface ListFilesParams {
  category?: FileCategory | 'all'
  storage?: StorageBackend | 'all'
  q?: string
}

export async function listFiles(params: ListFilesParams = {}): Promise<FileRecord[]> {
  if (USE_MOCK) {
    await delay(200)
    let files = loadFiles()
    if (params.category && params.category !== 'all') {
      files = files.filter((f) => f.category === params.category)
    }
    if (params.storage && params.storage !== 'all') {
      files = files.filter((f) => f.storage === params.storage)
    }
    if (params.q?.trim()) {
      const q = params.q.trim().toLowerCase()
      files = files.filter(
        (f) =>
          f.filename.toLowerCase().includes(q) ||
          f.trace_id.toLowerCase().includes(q) ||
          (f.zone?.toLowerCase().includes(q) ?? false),
      )
    }
    return files
  }

  const qs = new URLSearchParams()
  if (params.category && params.category !== 'all') qs.set('category', params.category)
  if (params.storage && params.storage !== 'all') qs.set('storage', params.storage)
  if (params.q) qs.set('q', params.q)
  const res = await apiFetch(`/files?${qs.toString()}`)
  if (!res.ok) throw new Error(`Listare fișiere eșuată: ${res.status}`)
  return (await res.json()) as FileRecord[]
}

export async function downloadFile(id: string): Promise<DownloadResult> {
  if (USE_MOCK) {
    await delay(400)
    const files = loadFiles()
    const file = files.find((f) => f.id === id)
    if (!file) throw new Error('Fișier negăsit')

    // Rare corruption simulation for demo (disabled by default — set flag)
    const forceCorrupt = sessionStorage.getItem('filegrid_force_corrupt') === id
    if (forceCorrupt) {
      const err = new Error('409 Conflict / File Corrupted') as Error & { corrupted: boolean }
      err.corrupted = true
      throw err
    }

    const blob = file.content_b64
      ? base64ToBlob(file.content_b64)
      : new Blob([`FileGrid mock content for ${file.filename}`])

    addAudit(file.id, 'download', 'Integritate verificată byte-for-byte')
    return {
      blob,
      filename: file.filename,
      integrityVerified: true,
      sha256: file.sha256,
      corrupted: false,
    }
  }

  const res = await apiFetch(`/files/${id}/download`)
  if (res.status === 409) {
    const err = new Error('409 Conflict / File Corrupted') as Error & { corrupted: boolean }
    err.corrupted = true
    throw err
  }
  if (!res.ok) throw new Error(`Download eșuat: ${res.status}`)

  const integrityVerified = res.headers.get('X-Integrity-Verified') === 'true'
  const sha256 = res.headers.get('X-File-Sha256') || ''
  const blob = await res.blob()
  const filename =
    filenameFromDisposition(res.headers.get('Content-Disposition')) || `file-${id}`

  return { blob, filename, integrityVerified, sha256 }
}

export async function getAuditLog(fileId: string): Promise<AuditEntry[]> {
  if (USE_MOCK) {
    await delay(150)
    return loadAudit().filter((e) => e.file_id === fileId)
  }
  const res = await apiFetch(`/files/${fileId}/audit`)
  if (!res.ok) throw new Error(`Audit eșuat: ${res.status}`)
  return (await res.json()) as AuditEntry[]
}

// ─── Policies ───────────────────────────────────────────────────────────────

export async function getPolicies(): Promise<PoliciesMap> {
  if (USE_MOCK) {
    await delay(150)
    return loadPolicies()
  }
  const res = await apiFetch('/admin/policies')
  if (!res.ok) throw new Error(`Politici eșuate: ${res.status}`)
  return (await res.json()) as PoliciesMap
}

export async function updatePolicies(policies: PoliciesMap): Promise<PoliciesMap> {
  if (USE_MOCK) {
    await delay(300)
    savePolicies(policies)
    return policies
  }
  const res = await apiFetch('/admin/policies', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(policies),
  })
  if (!res.ok) throw new Error(`Salvare politici eșuată: ${res.status}`)
  return (await res.json()) as PoliciesMap
}

export type { CategoryPolicy }

// ─── Analytics ──────────────────────────────────────────────────────────────

export async function getAnalyticsSummary(): Promise<AnalyticsSummary> {
  if (USE_MOCK) {
    await delay(250)
    return {
      storage_matrix: [
        {
          category: 'meter_readings',
          storage: 'postgres',
          latency_avg_ms: 8,
          latency_p95_ms: 11,
          throughput_mbps: 12.4,
          error_rate: 0.02,
          verdict:
            'PostgreSQL este optim pentru citiri contoare (<50KB) datorită latenței p95 de 11ms și constrângerii de unicitate.',
        },
        {
          category: 'outage_records',
          storage: 's3',
          latency_avg_ms: 45,
          latency_p95_ms: 78,
          throughput_mbps: 85.2,
          error_rate: 0.05,
          verdict:
            'S3 oferă durabilitate ridicată pentru rapoartele de avarie și scalare rapidă la vârfuri de furtună.',
        },
        {
          category: 'connection_docs',
          storage: 'ftp',
          latency_avg_ms: 120,
          latency_p95_ms: 210,
          throughput_mbps: 18.6,
          error_rate: 0.08,
          verdict:
            'FTP cu AES-256-GCM rămâne canalul conform pentru documente de racordare sensibile (CI, avize).',
        },
        {
          category: 'drone_videos',
          storage: 's3',
          latency_avg_ms: 220,
          latency_p95_ms: 410,
          throughput_mbps: 156.0,
          error_rate: 0.12,
          verdict:
            'S3 Multipart oferă throughput de 4.5x mai mare pentru video-uri dronă >100MB față de stocare relațională.',
        },
      ],
      load_comparison: [
        {
          label: 'Zi normală',
          meter_readings_ms: 11,
          outage_records_ms: 48,
          connection_docs_ms: 125,
          drone_videos_ms: 230,
        },
        {
          label: 'Vârf furtună (10x)',
          meter_readings_ms: 28,
          outage_records_ms: 95,
          connection_docs_ms: 132,
          drone_videos_ms: 245,
        },
      ],
      zone_insights: ZONE_OPTIONS.map((zone) => {
        const demo: Record<string, { outages_reported: number; meter_anomalies: number; drone_inspections: number; drone_status: 'completed' | 'in_progress' | 'pending' }> = {
          Prahova: { outages_reported: 47, meter_anomalies: 12, drone_inspections: 8, drone_status: 'completed' },
          Constanța: { outages_reported: 31, meter_anomalies: 5, drone_inspections: 4, drone_status: 'in_progress' },
          Timiș: { outages_reported: 18, meter_anomalies: 3, drone_inspections: 6, drone_status: 'pending' },
        }
        const sample = demo[zone]
        return sample
          ? { zone, ...sample }
          : {
              zone,
              outages_reported: 0,
              meter_anomalies: 0,
              drone_inspections: 0,
              drone_status: 'pending' as const,
            }
      }),
      narrative: {
        storage_verdict:
          'PostgreSQL este optim pentru citiri contoare (<50KB) datorită latenței p95 de 11ms și constrângerii de unicitate; S3 Multipart oferă throughput de 4.5x mai mare pentru video-uri dronă >100MB.',
        storm_isolation:
          'La creșterea de 10× a volumului pe contoare/avarii, latența documentelor de racordare și a video-urilor dronă rămâne izolată (+5–7%), confirmând izolarea pe cozi și priorități.',
        network:
          'Date de demonstrație. Pe backendul real, detectorul compară tensiunea, evenimentele de tamper și puterea cu sim_label.',
      },
    }
  }

  const res = await apiFetch('/analytics/summary')
  if (!res.ok) throw new Error(`Analytics eșuat: ${res.status}`)
  return (await res.json()) as AnalyticsSummary
}

// ─── Chaos console (admin only, off unless CHAOS_ENABLED=true on the backend) ──────────

export interface ChaosFault {
  target: string // s3 | ftp | postgres | network
  kind: 'down' | 'slow' | 'bad'
  seconds: number
  created_by: string
  expires_at: string
  remaining_seconds: number
}

export interface ChaosStatus {
  enabled: boolean
  max_seconds: number
  default_seconds: number
  faults?: ChaosFault[]
  /** null = toxiproxy could not be reached (unknown) */
  network_degraded?: boolean | null
  storages?: Record<string, { circuit: 'closed' | 'open' | 'half_open' | string }>
  recent?: { id: string; user: string; details?: string; timestamp?: string }[]
}

async function chaosCall(path: string, method: string, body?: unknown): Promise<unknown> {
  const res = await apiFetch(`/admin/chaos${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    let detail = ''
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? ''
    } catch {
      // not JSON
    }
    throw new Error(detail || `Cerere eșuată: ${res.status}`)
  }
  return res.json()
}

export async function getChaosStatus(): Promise<ChaosStatus> {
  if (USE_MOCK) return { enabled: false, max_seconds: 600, default_seconds: 60 }
  return (await chaosCall('', 'GET')) as ChaosStatus
}

export async function breakStorage(
  name: string,
  kind: 'down' | 'slow',
  durationSeconds: number,
  slowSeconds = 2,
): Promise<void> {
  await chaosCall(`/storage/${name}`, 'PUT', {
    kind,
    duration_seconds: durationSeconds,
    ...(kind === 'slow' ? { seconds: slowSeconds } : {}),
  })
}

export async function healStorage(name: string): Promise<void> {
  await chaosCall(`/storage/${name}`, 'DELETE')
}

export async function setNetwork(mode: 'bad' | 'ok', durationSeconds = 30): Promise<void> {
  await chaosCall('/network', 'PUT', {
    mode,
    ...(mode === 'bad' ? { duration_seconds: durationSeconds } : {}),
  })
}

export async function clearChaos(): Promise<void> {
  await chaosCall('', 'DELETE')
}

// ─── In-app alerts (Prometheus alerts + the application's own, e.g. failover) ─────────────

export interface AppAlert {
  id: string
  source: 'prometheus' | 'app'
  severity: 'critical' | 'warning' | 'info' | string
  title: string
  detail: string
  runbook?: string | null
  category?: string | null
  storage?: string | null
  since?: string | null
  state: 'firing' | 'pending' | 'resolved' | string
  /** how many alerts of this kind are merged into this card */
  count?: number
  /** what the merged alerts are about, e.g. 'outage_records · s3' */
  targets?: string[]
}

export interface AlertsResponse {
  alerts: AppAlert[]
  counts: { critical: number; warning: number; info: number }
  prometheus_ok: boolean
}

export async function getAlerts(): Promise<AlertsResponse> {
  if (USE_MOCK) {
    return { alerts: [], counts: { critical: 0, warning: 0, info: 0 }, prometheus_ok: true }
  }
  const res = await apiFetch('/alerts')
  if (!res.ok) throw new Error(`Alerte indisponibile: ${res.status}`)
  return (await res.json()) as AlertsResponse
}

// ─── Testing overlay (admin only, off unless TESTING_ENABLED=true) + live storage latency ───

export interface TestingStatus {
  enabled: boolean
  quiet?: { on: boolean; remaining_seconds: number }
  generator?: { on: boolean; categories: string[]; rate: number; remaining_seconds: number }
  simulator?: {
    mode: string
    target_rate: number
    paused: boolean
    meters: number
    backlog: number
    counts: Record<string, number>
  } | null
  job?: {
    name: string
    running: boolean
    summary?: string
    error?: string
    started?: number
    finished?: number
  } | null
  jobs?: Record<string, string>
  limits?: { max_rate: number; max_duration: number }
}

export interface StorageLive {
  put_p50_ms?: number
  put_p95_ms?: number
  get_p50_ms?: number
  get_p95_ms?: number
  ops_per_s?: number
}

export interface LiveLatency {
  available: boolean
  storages: Record<string, StorageLive>
  at: number
}

async function testingCall(path: string, method: string, body?: unknown): Promise<unknown> {
  const res = await apiFetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    let detail = ''
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? ''
    } catch {
      // not JSON
    }
    throw new Error(detail || `Cerere eșuată: ${res.status}`)
  }
  return res.json()
}

export async function getTestingStatus(): Promise<TestingStatus> {
  if (USE_MOCK) return { enabled: false }
  return (await testingCall('/admin/testing', 'GET')) as TestingStatus
}

export async function setGenerator(
  on: boolean,
  categories: string[],
  rate: number,
  durationSeconds: number,
): Promise<void> {
  await testingCall(
    '/admin/testing/generator',
    'PUT',
    on ? { on, categories, rate, duration_seconds: durationSeconds } : { on },
  )
}

export async function setQuiet(on: boolean, durationSeconds = 1800): Promise<void> {
  await testingCall('/admin/testing/quiet', 'PUT', on ? { on, duration_seconds: durationSeconds } : { on })
}

export async function simulatorAction(action: 'storm' | 'normal' | 'pause' | 'resume'): Promise<void> {
  await testingCall('/admin/testing/simulator', 'POST', { action })
}

export async function startTestJob(name: string): Promise<void> {
  await testingCall(`/admin/testing/jobs/${name}`, 'POST')
}

export async function getLiveLatency(): Promise<LiveLatency> {
  if (USE_MOCK) return { available: false, storages: {}, at: Date.now() / 1000 }
  return (await testingCall('/analytics/live', 'GET')) as LiveLatency
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}
