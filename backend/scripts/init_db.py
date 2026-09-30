import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.seed import seed_database

if __name__ == "__main__":
    seed_database()
    print("Database tables created and sample data seeded (safe to rerun).")
