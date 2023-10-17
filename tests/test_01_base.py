from ozonenv.core.db.mongodb_utils import (
    connect_to_mongo,
    close_mongo_connection,
    DbSettings,
)

from ozonenv_app.core.ozon.OzonEnvApp import OzonEnvApp
from test_common import *

pytestmark = pytest.mark.asyncio


@pytestmark
async def test_0_setup():
    init_env_var()
    ozon = OzonEnvApp()
    await ozon.env.init_orm()
    # setting up base config
    await setup_config(ozon.env)
    await ozon.env.orm.init_models()
    ozon.env.params = {"current_session_token": "BA6BA930"}
    await ozon.env.session_app()
    assert len(ozon.env.models) >= 4
    assert 'session' in ozon.env.models
    await ozon.env.close_env()


@pytestmark
async def test_1_setup():
    init_env_var()
    ozon = OzonEnvApp()
    res = await ozon.new(current_session_token="BA6BA9")
    assert res.status == "error"
    assert res.message == "Authentication error"
    await ozon.env.close_env()


@pytestmark
async def test_2_sutup_with_db():
    config_system = {
        "mongo_user": os.getenv("MONGO_USER"),
        "mongo_pass": os.getenv("MONGO_PASS"),
        "mongo_url": os.getenv("MONGO_URL"),
        "mongo_db": os.getenv("MONGO_DB"),
        "mongo_replica": os.getenv("MONGO_REPLICA"),
    }

    db_settings = DbSettings(**config_system)
    db = await connect_to_mongo(db_settings)
    ozon = OzonEnvApp()
    await ozon.new_app(db=db)
    res = await ozon.app_run_session(current_session_token="BA6BA930")
    assert res.fail is False
    await init_main_collections(ozon.env)
    assert len(ozon.env.models) == 20
    assert 'session' in ozon.env.models
    assert ozon.env.app_code == os.getenv("APP_CODE")
    data = await get_user_data()
    user_data = data[0]
    user_model = ozon.env.get('user')
    pw_hash = pwd_context.hash(user_data['password'])
    user_data['password'] = pw_hash
    await user_model.upsert(user_data)
    await ozon.env.close_env()
    await close_mongo_connection()

