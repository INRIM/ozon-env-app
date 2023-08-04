import os

from ozonenv.core.db.mongodb_utils import (
    db,
    connect_to_mongo,
    close_mongo_connection,
    DbSettings,
    Mongo
)


# async def get_database() -> AsyncIOMotorDatabase:
#     return db.engine
#
#
# async def get_client() -> AsyncIOMotorClient:
#     return db.client
def get_db() -> Mongo:
    return db


async def connect_db():
    config_system = {
        "mongo_user": os.getenv("MONGO_USER"),
        "mongo_pass": os.getenv("MONGO_PASS"),
        "mongo_url": os.getenv("MONGO_URL"),
        "mongo_db": os.getenv("MONGO_DB"),
        "mongo_replica": os.getenv("MONGO_REPLICA"),
    }
    db_settings = DbSettings(**config_system)
    await connect_to_mongo(db_settings)


async def close_db():
    await close_mongo_connection()
