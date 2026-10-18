import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _flag(name, default):
    return os.getenv(name, default).strip().lower() in ("1", "true", "on", "yes")


@dataclass(frozen=True)
class Settings:
    falkor_host: str = os.getenv("FALKORDB_HOST", "localhost")
    falkor_port: int = int(os.getenv("FALKORDB_PORT", "6379"))
    falkor_password: str = os.getenv("FALKORDB_PASSWORD", "")
    graph_prefix: str = os.getenv("GRAPH_PREFIX", "sev0_")
    default_tenant: str = os.getenv("DEFAULT_TENANT", "kirana")
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    deploy_lookback_min: int = int(os.getenv("DEPLOY_LOOKBACK_MIN", "120"))
    max_agent_steps: int = int(os.getenv("MAX_AGENT_STEPS", "10"))
    simulator_enabled: bool = _flag("SIMULATOR", "on")
    simulator_rps: int = int(os.getenv("SIMULATOR_RPS", "10"))
    detector_interval_s: int = int(os.getenv("DETECTOR_INTERVAL_S", "5"))
    edge_ttl_s: int = int(os.getenv("EDGE_TTL_S", "900"))
    api_keys_raw: str = os.getenv("SEV0_API_KEYS", "")
    webhook_secret: str = os.getenv("DEPLOY_WEBHOOK_SECRET", "")
    investigate_rate_per_min: int = int(os.getenv("INVESTIGATE_RATE_PER_MIN", "10"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


settings = Settings()
