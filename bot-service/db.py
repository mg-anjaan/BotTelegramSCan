# db.py - minimal SQLAlchemy async setup for Postgres
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Float, DateTime, func, select
from sqlalchemy.dialects.postgresql import JSONB

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

engine = create_async_engine(DATABASE_URL, future=True)
AsyncSessionLocal = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
Base = declarative_base()

class Offense(Base):
    __tablename__ = "offenses"
    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(String, index=True)
    user_id = Column(String, index=True)
    msg_id = Column(String)
    score = Column(Float)
    action = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Cache(Base):
    __tablename__ = "cache"
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True)
    score = Column(Float)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Whitelist(Base):
    __tablename__ = "whitelist"
    id = Column(Integer, primary_key=True)
    user_id = Column(String, unique=True, index=True)
    note = Column(String)

async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_cached_score(session, key: str):
    q = select(Cache).where(Cache.key == key)
    res = await session.execute(q)
    return res.scalars().first()
