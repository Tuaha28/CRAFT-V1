from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from utils.config import DATABASE_URL
from utils.logger import get_logger

logger = get_logger("database", "events.log")

# Create engine with connection pooling optimized for heavy AI reads/writes
try:
    engine = create_engine(
        DATABASE_URL,
        pool_size=10,          # Keep 10 connections open
        max_overflow=20,       # Allow up to 20 extra during traffic spikes
        pool_pre_ping=True,    # Verify connection is alive before querying
        echo=False             # Set to True if you want to see raw SQL in the terminal
    )
    logger.info("Database engine initialized with connection pooling.")
except Exception as e:
    logger.critical(f"Failed to initialize database engine: {e}")
    raise

# Session factory for transactions
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db_session():
    """
    Dependency generator that provides a database session and safely closes it.
    Used primarily for writing schedules back to the database.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_engine():
    """
    Returns the raw engine. 
    Highly useful for fast Pandas read_sql queries (Graph building).
    """
    return engine