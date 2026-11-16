import json
import os
from pathlib import Path
from dataclasses import dataclass, field
from urllib.parse import urlparse
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

@dataclass
class Settings:
    database: str = field(default_factory=lambda: os.getenv('DATABASE_PATH', str(ROOT/'data/runtime.sqlite3')))
    mode: str = field(default_factory=lambda: os.getenv('LLM_MODE', 'offline'))
    llm_url: str = field(default_factory=lambda: os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1').rstrip('/'))
    llm_key: str = field(default_factory=lambda: os.getenv('LLM_API_KEY', ''))
    llm_model: str = field(default_factory=lambda: os.getenv('LLM_MODEL', ''))
    backend_url: str = field(default_factory=lambda: os.getenv('BACKEND_URL', '').rstrip('/'))
    backend_key: str = field(default_factory=lambda: os.getenv('BACKEND_SERVICE_KEY', ''))
    tavily_key: str = field(default_factory=lambda: os.getenv('TAVILY_API_KEY', ''))
    tokens: dict = field(default_factory=lambda: json.loads(os.getenv('CUSTOMER_TOKENS', '{"demo-customer-1":"CUST-001","demo-customer-2":"CUST-002","demo-visitor":"guest"}')))
    allowed_origins: list = field(default_factory=lambda: os.getenv('CORS_ORIGINS','http://localhost:8501').split(','))
    max_steps: int = 6
    environment: str = field(default_factory=lambda: os.getenv('APP_ENV','demo'))
    auth_mode: str = field(default_factory=lambda: os.getenv('AUTH_MODE','demo'))
    jwt_key: str = field(default_factory=lambda: os.getenv('JWT_VERIFICATION_KEY','').replace('\\n','\n'))
    jwt_algorithm: str = field(default_factory=lambda: os.getenv('JWT_ALGORITHM','HS256'))
    jwt_issuer: str = field(default_factory=lambda: os.getenv('JWT_ISSUER','needlecraft-portal'))
    jwt_audience: str = field(default_factory=lambda: os.getenv('JWT_AUDIENCE','needlecraft-agent'))
    metrics_token: str = field(default_factory=lambda: os.getenv('METRICS_TOKEN',''))
    allow_reference: bool = field(default_factory=lambda: os.getenv('ALLOW_REFERENCE_DATA','false').lower()=='true')
    require_semantic: bool = field(default_factory=lambda: os.getenv('REQUIRE_SEMANTIC_INDEX','false').lower()=='true')
    require_grounding: bool = field(default_factory=lambda: os.getenv('REQUIRE_GROUNDED_OUTPUT','true').lower()=='true')
    max_chat_seconds: float = 90
    max_tool_calls: int = 12
    rate_per_minute: int = field(default_factory=lambda: int(os.getenv('RATE_PER_MINUTE','30')))
    daily_chats: int = field(default_factory=lambda: int(os.getenv('DAILY_CHATS_PER_CUSTOMER','200')))
    global_concurrency: int = field(default_factory=lambda: int(os.getenv('GLOBAL_CHAT_CONCURRENCY','8')))
    max_body_bytes: int = 32768
    conversation_days: int = 30
    audit_days: int = 90

    def __post_init__(self):
        # Local configuration retains the existing reference-data implementation.
        if self.environment == 'local':
            self.environment = 'demo'
        if self.auth_mode == 'local':
            self.auth_mode = 'demo'

    def validate(self):
        if self.environment not in ('demo','production') or self.auth_mode not in ('demo','jwt'):
            raise ValueError('APP_ENV must be local or production; AUTH_MODE must be local or jwt (legacy demo values are supported)')
        if self.mode not in ('offline','llm'):
            raise ValueError('LLM_MODE must be offline or llm')
        if self.mode=='llm' and not self.llm_model:
            raise ValueError('Set LLM_MODEL when LLM_MODE=llm')
        if self.backend_url and not self.backend_key:
            raise ValueError('BACKEND_SERVICE_KEY is required with BACKEND_URL')
        if self.auth_mode=='demo' and not self.tokens:
            raise ValueError('Configure CUSTOMER_TOKENS')
        if self.auth_mode=='jwt':
            if self.jwt_algorithm not in ('HS256','RS256') or not self.jwt_key:
                raise ValueError('JWT verification key and an allowed JWT algorithm are required')
            if self.jwt_algorithm=='HS256' and len(self.jwt_key.encode())<32:
                raise ValueError('Use a JWT secret of at least 32 bytes')
            if self.jwt_algorithm=='RS256' and 'PUBLIC KEY' not in self.jwt_key:
                raise ValueError('RS256 requires the portal public verification key, not its private key')
        for base in [self.llm_url,self.backend_url]:
            if base and (urlparse(base).scheme not in ('https','http') or not urlparse(base).hostname or urlparse(base).username or urlparse(base).query or urlparse(base).fragment):
                raise ValueError('Provider addresses must be HTTP(S) URLs without embedded credentials')
        if min(self.rate_per_minute,self.daily_chats,self.global_concurrency,self.max_steps,self.max_tool_calls)<=0:
            raise ValueError('Resource limits must be positive')
        if self.environment=='production':
            if self.auth_mode!='jwt' or self.mode!='llm' or not self.backend_url:
                raise ValueError('Production requires JWT authentication, a configured LLM and the main backend')
            if not self.require_grounding:
                raise ValueError('Production requires grounded structured model outputs')
            if len(self.metrics_token)<32:
                raise ValueError('Production requires a private METRICS_TOKEN of at least 32 characters')
            if len(self.backend_key)<32:
                raise ValueError('Production requires a backend service key of at least 32 characters')
            if '*' in self.allowed_origins:
                raise ValueError('Configure specific CORS origins')
            if urlparse(self.llm_url).hostname=='api.openai.com' and not self.llm_key:
                raise ValueError('Configure LLM_API_KEY for the hosted model')

