import sqlite3

path = r"C:\Users\josel\Documents\STAGING\DB\industrials.sqlite"
connection = sqlite3.connect(path)
connection.row_factory = sqlite3.Row
rows = connection.execute(
    """
    SELECT issue_id, detected_at, severity, stage, source_id, issue_type,
           issue_detail, resolution_status
    FROM data_quality_issues
    WHERE UPPER(COALESCE(ticker, '')) = 'CGI'
    ORDER BY issue_id
    """
).fetchall()
print(f"rows={len(rows)}")
for row in rows:
    print(dict(row))
connection.close()
