-- Frozen schema-v9 checkpoint DDL; preserve original strict constraints.

            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_updates (
              daily_id TEXT PRIMARY KEY, target_date TEXT NOT NULL, universe_id TEXT NOT NULL,
              daily_update_id TEXT NOT NULL, execution_date TEXT NOT NULL,
              preflight_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(preflight_status IN ('pending','validated')),
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','running','completed','failed'))
            );
            CREATE TABLE IF NOT EXISTS dataset_snapshots (
              daily_id TEXT NOT NULL REFERENCES daily_updates(daily_id), dataset_key TEXT NOT NULL,
              snapshot_id TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','running','completed','failed')),
              PRIMARY KEY(daily_id,dataset_key)
            );
            CREATE TABLE IF NOT EXISTS snapshot_sequences (
              daily_id TEXT NOT NULL REFERENCES daily_updates(daily_id), symbol TEXT NOT NULL,
              dataset_key TEXT NOT NULL,
              sequence_no INTEGER NOT NULL CHECK(sequence_no BETWEEN 1 AND 99999),
              sequence_count INTEGER NOT NULL
                CHECK(sequence_count BETWEEN sequence_no AND 99999),
              attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
              lease_until TEXT, snapshot BLOB CHECK(snapshot IS NULL OR length(snapshot) BETWEEN 1 AND 1048576),
              raw_ref TEXT, raw_sha256 TEXT, raw_size INTEGER, prepared BLOB,
              prepared_sha256 TEXT,
              source_attempt_id TEXT, source_run_id TEXT,
              terminal_status TEXT CHECK(terminal_status IS NULL OR length(terminal_status) BETWEEN 1 AND 64),
              terminal_reason TEXT CHECK(terminal_reason IS NULL OR terminal_reason IN ('child_boundary','contract_access','contract_mapping','ipc_encode','ipc_schema','ipc_size','ipc_transport','kbars_scalar','kbars_shape','snapshot')),
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN (
                  'pending','acquiring','acquired','raw_uploading','raw_persisted',
                  'prepared','delivered','terminal','failed'
                )),
              CHECK(
                (raw_ref IS NULL AND raw_sha256 IS NULL AND raw_size IS NULL)
                OR (raw_ref IS NULL AND raw_sha256 IS NOT NULL AND raw_size IS NULL)
                OR (raw_ref IS NOT NULL AND raw_sha256 IS NOT NULL AND raw_size BETWEEN 1 AND 1048576)
              ),
              CHECK(prepared IS NULL OR (raw_ref IS NOT NULL AND raw_sha256 IS NOT NULL)),
              CHECK(
                (prepared IS NULL AND prepared_sha256 IS NULL)
                OR (prepared IS NOT NULL AND length(prepared_sha256) = 64)
              ),
              CHECK((source_attempt_id IS NULL) = (source_run_id IS NULL)),
              CHECK(terminal_reason IS NULL OR terminal_status='KBARS_PAYLOAD'),
              PRIMARY KEY(daily_id,symbol), UNIQUE(daily_id,dataset_key,sequence_no)
            );
            CREATE TABLE IF NOT EXISTS provider_attempts (
              daily_id TEXT NOT NULL, symbol TEXT NOT NULL,
              attempt_no INTEGER NOT NULL CHECK(attempt_no BETWEEN 1 AND 3),
              created_at TEXT NOT NULL,
              outcome TEXT NOT NULL DEFAULT 'started'
                CHECK(outcome IN ('started','success','failed')),
              code TEXT CHECK(code IS NULL OR length(code) BETWEEN 1 AND 64),
              reason TEXT CHECK(reason IS NULL OR reason IN ('child_boundary','contract_access','contract_mapping','ipc_encode','ipc_schema','ipc_size','ipc_transport','kbars_scalar','kbars_shape','snapshot')),
              CHECK(reason IS NULL OR code='KBARS_PAYLOAD'),
              PRIMARY KEY(daily_id,symbol,attempt_no),
              FOREIGN KEY(daily_id,symbol) REFERENCES snapshot_sequences(daily_id,symbol)
            );
            CREATE TABLE IF NOT EXISTS limiter (used_at TEXT NOT NULL);
            
INSERT INTO meta VALUES ('schema_version','9');
