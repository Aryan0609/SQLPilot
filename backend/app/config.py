from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://sqlpilot_writer:change_me@localhost:5432/sqlpilot"
    readonly_database_url: str = "postgresql+psycopg://sqlpilot_reader:change_readonly@localhost:5432/sqlpilot"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:3b"
    max_rows: int = 200
    query_timeout_ms: int = 15000
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

settings = Settings()
