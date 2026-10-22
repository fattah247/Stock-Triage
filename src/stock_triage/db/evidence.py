"""Rebuilding a document's evidence spans without breaking the rows that cite them."""

from __future__ import annotations

import sqlite3


def release_evidence_spans(conn: sqlite3.Connection, document_id: str) -> None:
    """Delete ``document_id``'s evidence spans, unlinking every row that cites one.

    Re-parsing or re-extracting a document rebuilds its spans, but derived rows
    (claims, timeseries, nuance findings, ...) keep citing the old ones until
    the next intelligence run regenerates them. Their foreign keys would
    otherwise reject the delete. Referencing columns are found from the schema,
    so upstream stages need not know which downstream tables cite evidence.
    """
    references = conn.execute(
        """
        SELECT m.name AS table_name, fk."from" AS column_name
        FROM sqlite_master m
        JOIN pragma_foreign_key_list(m.name) fk
        WHERE m.type = 'table' AND fk."table" = 'evidence_spans'
        """
    ).fetchall()
    for table_name, column_name in references:
        conn.execute(
            f'UPDATE "{table_name}" SET "{column_name}" = NULL '
            f'WHERE "{column_name}" IN (SELECT span_id FROM evidence_spans WHERE document_id = ?)',
            (document_id,),
        )
    conn.execute("DELETE FROM evidence_spans WHERE document_id = ?", (document_id,))
