import sqlite3
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    kb_dir = data_dir / "kb"
    kb_dir.mkdir(exist_ok=True)
    output_dir = data_dir / "output"
    output_dir.mkdir(exist_ok=True)

    db_path = data_dir / "company.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS customers ("
        "id INTEGER PRIMARY KEY, name TEXT, email TEXT, region TEXT)"
    )
    conn.execute("DELETE FROM customers")
    conn.executemany(
        "INSERT INTO customers (name, email, region) VALUES (?, ?, ?)",
        [
            ("Alice", "alice@company.com", "cn"),
            ("Bob", "bob@company.com", "cn"),
            ("Eve", "eve@external.com", "us"),
        ],
    )
    conn.commit()
    conn.close()
    print(f"demo db ready: {db_path}")


if __name__ == "__main__":
    main()
