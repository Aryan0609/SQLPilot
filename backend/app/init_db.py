from .db import writer_engine
from .dataset_models import DatasetBase
from .models import Base
from sqlalchemy import text

if __name__ == "__main__":
    Base.metadata.create_all(writer_engine)
    DatasetBase.metadata.create_all(writer_engine)
    with writer_engine.begin() as conn:
        conn.execute(text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO sqlpilot_reader"))
        conn.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO sqlpilot_reader"))
    print("Schema initialized and reader grants applied.")
