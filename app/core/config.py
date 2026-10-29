from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://courtbook:courtbook@localhost:5432/courtbook"
    redis_enabled: bool = False
    redis_url: str = "redis://localhost:6379/0"
    hold_ttl_seconds: int = 300
    app_env: str = "development"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
