export type FileCategory =
  | 'meter_readings'
  | 'outage_records'
  | 'connection_docs'
  | 'drone_videos'

export type StorageBackend = 'postgres' | 's3' | 'ftp'

export type QueuePriority = 'high' | 'normal' | 'low'

export type Zone = 'Muntenia' | 'Banat' | 'Dobrogea'

export interface FileMetadata {
  zone?: Zone | string
  station?: string
  description?: string
  [key: string]: unknown
}

export interface FileRecord {
  id: string
  filename: string
  category: FileCategory
  storage: StorageBackend
  size_bytes: number
  sha256: string
  created_at: string
  encrypted: boolean
  trace_id: string
  zone?: string
  station?: string
  description?: string
  uploaded_by?: string
  /** Base64 content for mock download (small files / metadata only for large) */
  content_b64?: string
}

export interface IngestResponse {
  file_id: string
  trace_id: string
  sha256: string
  storage: StorageBackend
}

export interface ChunkUploadResponse {
  upload_id: string
  received_chunks: number
  total_chunks?: number
  /** 'complete' on the chunk that finished the file; then the file fields are set */
  status?: 'open' | 'complete'
  file_id?: string
  trace_id?: string
  sha256?: string
  storage?: StorageBackend
}

export interface CategoryPolicy {
  storage: StorageBackend
  encryption: boolean
  deduplicate: boolean
  priority: QueuePriority
  /** where new files go automatically if the primary storage fails or is overloaded */
  fallback_storage?: StorageBackend | null
}

export type PoliciesMap = Record<FileCategory, CategoryPolicy>

export interface AuditEntry {
  id: string
  file_id: string
  action: 'upload' | 'download'
  user: string
  timestamp: string
  details?: string
}

export interface StorageCategoryMetrics {
  category: FileCategory
  storage: StorageBackend
  latency_avg_ms: number
  latency_p95_ms: number
  throughput_mbps: number
  error_rate: number
  verdict: string
}

export interface LoadComparisonPoint {
  label: string
  meter_readings_ms: number
  outage_records_ms: number
  connection_docs_ms: number
  drone_videos_ms: number
}

export interface ZoneInsight {
  zone: Zone
  outages_reported: number
  meter_anomalies: number
  drone_inspections: number
  drone_status: 'completed' | 'in_progress' | 'pending'
}

export interface AnalyticsSummary {
  storage_matrix: StorageCategoryMetrics[]
  load_comparison: LoadComparisonPoint[]
  zone_insights: ZoneInsight[]
  narrative: {
    storage_verdict: string
    storm_isolation: string
    /** Detector versus the simulator's sim_label. Omitted by older mocks. */
    network?: string
  }
}

export interface AuthUser {
  email: string
  role: 'dispecer' | 'admin'
  name: string
}

export interface AuthSession {
  token: string
  user: AuthUser
}

export interface UploadProgress {
  percent: number
  currentChunk: number
  totalChunks: number
  speedMBps: number
  status: 'idle' | 'hashing' | 'uploading' | 'retrying' | 'complete' | 'error'
  retryAttempt?: number
  message?: string
}

export interface DownloadResult {
  blob: Blob
  filename: string
  integrityVerified: boolean
  sha256: string
  corrupted?: boolean
}

export const CATEGORY_LABELS: Record<FileCategory, string> = {
  meter_readings: 'Citiri contoare',
  outage_records: 'Înregistrări de avarie',
  connection_docs: 'Documente de racordare',
  drone_videos: 'Video-uri inspecții dronă',
}

export const STORAGE_LABELS: Record<StorageBackend, string> = {
  postgres: 'Postgres',
  s3: 'S3',
  ftp: 'FTP',
}

export const ZONE_OPTIONS: Zone[] = ['Muntenia', 'Banat', 'Dobrogea']

export const CHUNK_SIZE = 5 * 1024 * 1024 // 5 MB
export const CHUNK_THRESHOLD = 5 * 1024 * 1024 // use chunked upload above 5 MB
