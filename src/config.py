import os


class Settings:
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/voicebot"
    )
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    CELERY_BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1")
    CELERY_RESULT_BACKEND: str = os.getenv(
        "CELERY_RESULT_BACKEND", "redis://localhost:6379/2"
    )

    # LLM configuration — used for ALL post-call analysis (no tiering)
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "openai")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-4o")
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "sk-mock-key-for-assessment")
    LLM_TOKENS_PER_MINUTE: int = int(os.getenv("LLM_TOKENS_PER_MINUTE", "90000"))
    LLM_REQUESTS_PER_MINUTE: int = int(os.getenv("LLM_REQUESTS_PER_MINUTE", "500"))

    # Recording configuration
    RECORDING_WAIT_SECONDS: int = 45  # Hardcoded sleep before fetching recording
    S3_BUCKET: str = os.getenv("S3_BUCKET", "voicebot-recordings")

    # Circuit breaker
    CIRCUIT_BREAKER_CAPACITY_THRESHOLD: float = 0.90
    CIRCUIT_BREAKER_FREEZE_SECONDS: int = 1800

    # Post-call processing
    POSTCALL_CELERY_QUEUE: str = "postcall_processing"
    POSTCALL_MAX_RETRIES: int = 3
    POSTCALL_RETRY_DELAY: int = 60


settings = Settings()
