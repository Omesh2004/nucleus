import os
from enum import Enum
from pydantic_settings import BaseSettings

class DeploymentMode(str, Enum):
    CLOUD = "CLOUD"
    ON_PREM = "ON_PREM"

class Settings(BaseSettings):
    # Deployment Strategy
    DEPLOYMENT_MODE: DeploymentMode = DeploymentMode.CLOUD
    
    # On-Prem: identifies the single tenant this instance serves
    TENANT_ID: str = "default-app"
    
    # On-Prem: whether aggregate sync to central cloud is enabled
    SYNC_ENABLED: bool = False
    CENTRAL_API_URL: str = "http://cloud-api-placeholder"
    
    # Kafka
    KAFKA_BROKER_URL: str = "broker:29092"
    KAFKA_TOPIC_EVENTS: str = "feature-events"
    
    # ClickHouse
    CLICKHOUSE_HOST: str = "clickhouse"
    CLICKHOUSE_PORT: int = 8123
    CLICKHOUSE_USER: str = "default"
    CLICKHOUSE_PASSWORD: str = ""
    CLICKHOUSE_DATABASE: str = "feature_intelligence"
    
    # LLM Settings
    HF_TOKEN: str = ""
    VLLM_URL: str = "http://vllm-server:8000/v1"
    OLLAMA_URL: str = "http://ollama:11434"
    
    @property
    def is_cloud(self) -> bool:
        return self.DEPLOYMENT_MODE == DeploymentMode.CLOUD
    
    @property
    def is_on_prem(self) -> bool:
        return self.DEPLOYMENT_MODE == DeploymentMode.ON_PREM
    
    
    class Config:
        env_file = ".env"

# Global settings instance
settings = Settings()
