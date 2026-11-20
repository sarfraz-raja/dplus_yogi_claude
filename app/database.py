from sqlalchemy import create_engine
from urllib.parse import quote_plus
import os

SQL_HOST = os.getenv("SQL_HOST")
SQL_PORT = os.getenv("SQL_PORT")
SQL_DATABASE = os.getenv("SQL_DATABASE")
SQL_USERNAME = os.getenv("SQL_USERNAME")
SQL_PASSWORD = quote_plus(os.getenv("SQL_PASSWORD", ""))

DATABASE_URL = (
    f"postgresql+psycopg2://"
    f"{SQL_USERNAME}:{SQL_PASSWORD}@"
    f"{SQL_HOST}:{SQL_PORT}/{SQL_DATABASE}"
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20
)
