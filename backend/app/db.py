from sqlalchemy import create_engine
from .config import settings

writer_engine = create_engine(settings.database_url, pool_pre_ping=True)
reader_engine = create_engine(settings.readonly_database_url, pool_pre_ping=True)
