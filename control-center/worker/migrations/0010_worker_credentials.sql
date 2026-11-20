CREATE TABLE worker_credentials (
  worker_id TEXT PRIMARY KEY CHECK (worker_id IN ('blue','yellow')),
  scheme TEXT NOT NULL CHECK (scheme='sha256'),
  hash TEXT NOT NULL CHECK (length(hash)=64 AND hash NOT GLOB '*[^0-9a-f]*'),
  created_at INTEGER NOT NULL
);
