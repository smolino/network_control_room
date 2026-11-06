import os
import sys

# A throwaway SQLite DB: app.config reads DATABASE_URL at import time, so
# this has to be set before anything under app/ is imported.
os.environ["DATABASE_URL"] = "sqlite:////tmp/ncr-tests.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
