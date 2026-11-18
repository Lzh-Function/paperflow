import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from paperflow.ingestion.bibtex_reader import identity_key
from paperflow.models import Bibliography, digest
from paperflow.portable import bibliography

DDL = """
CREATE TABLE IF NOT EXISTS papers (
 paper_id TEXT PRIMARY KEY, doi_norm TEXT UNIQUE, arxiv_id TEXT, citekey TEXT NOT NULL,
 title TEXT NOT NULL, authors_json TEXT NOT NULL, year TEXT, abstract TEXT, bib_hash TEXT NOT NULL,
 metadata_json TEXT NOT NULL, bib_first_seen TEXT, bib_last_seen TEXT, source_present INTEGER,
 created_at TEXT, updated_at TEXT, state TEXT NOT NULL, match_json TEXT,
 source_hash TEXT, error_type TEXT, error_message TEXT, retry_count INTEGER DEFAULT 0,
 next_retry_at TEXT
);
CREATE TABLE IF NOT EXISTS identities (
 identity_key TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers(paper_id),
 PRIMARY KEY(identity_key,paper_id)
);
CREATE TABLE IF NOT EXISTS observations (
 source_path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, stable_count INTEGER
);
CREATE TABLE IF NOT EXISTS attachments (
 attachment_id TEXT PRIMARY KEY, paper_id TEXT REFERENCES papers(paper_id),
 source_path TEXT NOT NULL, size INTEGER, mtime_ns INTEGER, sha256 TEXT,
 is_primary INTEGER, match_score REAL, match_status TEXT,
 UNIQUE(paper_id,source_path)
);
CREATE TABLE IF NOT EXISTS analysis_runs (
 run_id TEXT PRIMARY KEY, paper_id TEXT REFERENCES papers(paper_id), provider TEXT,
 model TEXT, status TEXT, coverage TEXT, source_hash TEXT, schema_version TEXT,
 prompt_version TEXT, parser_version TEXT, output_json_path TEXT, output_json TEXT,
 error_type TEXT, error_message TEXT, started_at TEXT, ended_at TEXT,
 elapsed_seconds REAL, retryable INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS exports (
 paper_id TEXT REFERENCES papers(paper_id), export_kind TEXT, target_path TEXT,
 generated_hash TEXT, source_analysis_run_id TEXT, exported_at TEXT, status TEXT,
 PRIMARY KEY(paper_id, export_kind)
);
CREATE TABLE IF NOT EXISTS managed_files (path TEXT PRIMARY KEY, generated_hash TEXT, exported_at TEXT);
CREATE TABLE IF NOT EXISTS analysis_usage (
 run_id TEXT PRIMARY KEY REFERENCES analysis_runs(run_id), usage_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portable_analyses (
 run_id TEXT PRIMARY KEY REFERENCES analysis_runs(run_id), cache_key TEXT NOT NULL,
 input_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS portable_analyses_cache ON portable_analyses(cache_key);
CREATE TABLE IF NOT EXISTS analysis_aliases (
 run_id TEXT NOT NULL REFERENCES analysis_runs(run_id), source_hash TEXT NOT NULL,
 PRIMARY KEY(run_id,source_hash)
);
CREATE TABLE IF NOT EXISTS summary_versions (
 run_id TEXT PRIMARY KEY REFERENCES analysis_runs(run_id),
 paper_id TEXT NOT NULL REFERENCES papers(paper_id), pdf_sha256 TEXT NOT NULL,
 source_id TEXT NOT NULL, note_path TEXT NOT NULL, note_text TEXT NOT NULL,
 proposal_text TEXT NOT NULL, metadata_json TEXT NOT NULL, visuals_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
PRAGMA user_version=4;
"""


def now():
    return datetime.now(UTC).isoformat()


class IdentityConflict(ValueError):
    pass


class Store:
    def __init__(self, path: Path, *, readonly=False, snapshot=False):
        if snapshot:
            self.db = sqlite3.connect(":memory:")
            if path.exists():
                source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    source.backup(self.db)
                finally:
                    source.close()
        elif readonly:
            if not path.exists():
                self.db = sqlite3.connect(":memory:")
            else:
                self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 4:
            raise ValueError("このDBは新しいPaperFlowで作成されています")
        if version == 0 and (snapshot or not path.exists() or not readonly):
            self.db.executescript(DDL)
        elif version == 0 and readonly:
            # Only the in-memory empty status view needs schema creation.
            self.db.executescript(DDL)
        elif version in (1, 2, 3) and (snapshot or not readonly):
            self.db.executescript(DDL)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    @contextmanager
    def transaction(self):
        with self.db:
            yield

    def rows(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def paper(self, paper_id):
        rows = self.rows("SELECT * FROM papers WHERE paper_id=?", (paper_id,))
        if not rows:
            raise ValueError(f"文献IDが見つかりません: {paper_id}")
        return rows[0]

    def update(self, paper_id, **fields):
        # Column names are internal constants; all data values are bound parameters.
        allowed = {row[1] for row in self.db.execute("PRAGMA table_info(papers)")}
        if not fields.keys() <= allowed:
            raise ValueError("不正なDB列名")
        sql = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE papers SET {sql} WHERE paper_id=?", (*fields.values(), paper_id))

    def upsert_bib(self, bib: Bibliography) -> str:
        keys = [f"doi:{bib.doi}"] if bib.doi else []
        keys += [f"arxiv:{bib.arxiv_id}"] if bib.arxiv_id else []
        keys += [f"meta:{identity_key(bib)}"]
        ids = set()
        for key in keys:
            ids.update(
                r["paper_id"]
                for r in self.rows("SELECT paper_id FROM identities WHERE identity_key=?", (key,))
            )
        if len(ids) > 1:
            raise IdentityConflict(f"同一性キーが複数文献に衝突: {bib.citekey}")
        if not ids:
            # A stable citekey can bridge title corrections, but never conflicting DOI/arXiv IDs.
            same = self.rows("SELECT * FROM papers WHERE citekey=?", (bib.citekey,))
            if len(same) == 1:
                old = same[0]
                same_authors_year = (
                    json.loads(old["authors_json"]) == bib.authors and old["year"] == bib.year
                )
                if same_authors_year and not (
                    (bib.doi and old["doi_norm"] and bib.doi != old["doi_norm"])
                    or (bib.arxiv_id and old["arxiv_id"] and bib.arxiv_id != old["arxiv_id"])
                ):
                    ids.add(old["paper_id"])
        paper_id = next(iter(ids)) if ids else uuid.uuid4().hex[:16]
        existing = self.rows("SELECT * FROM papers WHERE paper_id=?", (paper_id,))
        if existing:
            old = existing[0]
            if (bib.doi and old["doi_norm"] and bib.doi != old["doi_norm"]) or (
                bib.arxiv_id and old["arxiv_id"] and bib.arxiv_id != old["arxiv_id"]
            ):
                raise IdentityConflict(f"識別子が既存文献と不一致: {bib.citekey}")
            # Missing values in a later export must not lose established identity.
            bib.doi = bib.doi or old["doi_norm"]
            bib.arxiv_id = bib.arxiv_id or old["arxiv_id"]
        metadata = json.dumps(bib.as_dict(), ensure_ascii=False)
        bib_hash = digest(bib.as_dict())
        stamp = now()
        if existing:
            old = existing[0]
            changed = bibliography(json.loads(old["metadata_json"])) != bibliography(bib.as_dict())
            state = "metadata_ready" if changed else old["state"]
            self.update(
                paper_id,
                doi_norm=bib.doi,
                arxiv_id=bib.arxiv_id,
                citekey=bib.citekey,
                title=bib.title,
                authors_json=json.dumps(bib.authors, ensure_ascii=False),
                year=bib.year,
                abstract=bib.abstract,
                bib_hash=bib_hash,
                metadata_json=metadata,
                bib_last_seen=stamp,
                source_present=1,
                updated_at=stamp if old["bib_hash"] != bib_hash else old["updated_at"],
                state=state,
                source_hash=None if changed else old["source_hash"],
            )
        else:
            self.db.execute(
                """INSERT INTO papers
                (paper_id,doi_norm,arxiv_id,citekey,title,authors_json,year,abstract,bib_hash,
                 metadata_json,bib_first_seen,bib_last_seen,source_present,created_at,updated_at,state)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    paper_id,
                    bib.doi,
                    bib.arxiv_id,
                    bib.citekey,
                    bib.title,
                    json.dumps(bib.authors, ensure_ascii=False),
                    bib.year,
                    bib.abstract,
                    bib_hash,
                    metadata,
                    stamp,
                    stamp,
                    1,
                    stamp,
                    stamp,
                    "metadata_ready",
                ),
            )
        for key in keys:
            self.db.execute("INSERT OR IGNORE INTO identities VALUES (?,?)", (key, paper_id))
        return paper_id

    def latest_analysis(self, paper_id, source_hash=None):
        query = "SELECT * FROM analysis_runs WHERE paper_id=? AND status='success'"
        params = [paper_id]
        if source_hash is not None:
            aliases = self.rows(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_aliases'"
            )
            if aliases:
                query += " AND (source_hash=? OR run_id IN (SELECT run_id FROM analysis_aliases WHERE source_hash=?))"
                params.extend([source_hash, source_hash])
            else:
                query += " AND source_hash=?"
                params.append(source_hash)
        results = self.rows(query + " ORDER BY started_at DESC, rowid DESC LIMIT 1", params)
        return results[0] if results else None

    def portable_analysis(self, paper_id, key):
        rows = self.rows(
            "SELECT a.* FROM analysis_runs a JOIN portable_analyses p USING(run_id) WHERE a.paper_id=? AND a.status='success' AND p.cache_key=? ORDER BY a.started_at DESC,a.rowid DESC LIMIT 1",
            (paper_id, key),
        )
        return rows[0] if rows else None
