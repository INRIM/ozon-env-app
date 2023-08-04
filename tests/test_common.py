import pytest
from ozonenv.OzonEnv import OzonEnv
from ozonenv.core.BaseModels import Dict
from passlib.context import CryptContext

from test_utils import *

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
BASE_DIR = Path(__file__).resolve().parent


@pytest.fixture
def anyio_backend():
    return 'asyncio'


# @pytest.fixture(scope="module")
# async def test_app():
#     async with AsyncClient(app=app, base_url="http://testspp") as client:
#         yield client

pytestmark = pytest.mark.asyncio

topic = "risolvi_anomalia_ordine"


def init_env_var():
    os.environ["APP_TITLE"] = "TEST"
    os.environ["APP_DESC"] = "TEST"
    os.environ["APP_VERSION"] = "3.0"
    os.environ["APP_CODE"] = "test"
    os.environ["STACK"] = "test"
    os.environ["MONGO_DB"] = "servicetest"
    os.environ["MONGO_USER"] = "servicetest"
    os.environ["MONGO_PASS"] = "servicetest"
    os.environ["MONGO_URL"] = "database:27017"
    os.environ["MONGO_REPLICA"] = ""
    os.environ["MODELS_FOLDER"] = "/models"
    os.environ["OZON_APPLANG"] = "it"
    os.environ["OZON_LOCALEDIR"] = ""
    os.environ["APP_TAMPLATE_DIR"] = f"/app/views/templates"
    os.environ["APP_WEB_STATIC"] = f"/app/views/static"
    # openssl rand -hex 32
    os.environ["JWT_SECRET_KEY"] = (
        "e2441a8680e6f07b104919d8" "4151512c981fb0d436b5e6b3e8685fb141f108ab"
    )
    os.environ["JWT_ALGORITHM"] = "HS256"
    os.environ["LDAP_PARAMS_NAME"] = "ldap"


def get_i18n_localedir():
    dirname = f"{Path(__file__).parent.absolute()}/i18n"
    Path(dirname).mkdir(parents=True, exist_ok=True)
    return dirname


def get_i18n_localedir_tr():
    return f"{Path(__file__).parent.absolute()}/i18n_translated"


def get_test_data_dir():
    return f"{Path(__file__).parent.absolute()}/testapp/base/data"


def get_base_data_dir():
    return get_test_data_dir()


@pytestmark
async def init_db_model(env: OzonEnv, data, collection):
    objects = data
    model = env.get(collection)
    if not await model.find({}):
        for obj_data in objects:
            obj = await model.new(obj_data)
            await model.insert(obj)


@pytestmark
async def init_db_collection(db, data, collection):
    coll = db.engine.get_collection(collection)
    objects = data
    datas = coll.find({})
    if not await datas.to_list(length=None):
        for obj in objects:
            await coll.insert_one(obj)


@pytestmark
async def init_db_component(env: OzonEnv, data):
    objects = data
    for schema in objects:
        await env.insert_update_component(schema)


@pytestmark
async def init_collecetion(env: OzonEnv, path, file_name, collection):
    data = await readjsonfile(path, file_name)
    if collection == "component":
        await init_db_component(env, data)
    else:
        if not isinstance(data, list):
            await init_db_collection(env.db, [data], collection)
        else:
            await init_db_collection(env.db, data, collection)


@pytestmark
async def setup_config(env):
    path = get_test_data_dir()
    await init_collecetion(env, path, 'coll_session.json', "session")
    await init_collecetion(env, path, 'config.json', "settings")
    await init_collecetion(env, path, 'menu_group.json', "menu_group")
    await init_collecetion(env, path, 'user_type.json', "user_type")
    await init_collecetion(env, path, 'action.json', "action")
    await init_collecetion(
        env, path, 'fast_search_config.json', "fast_search_config")


@pytestmark
async def get_user_data():
    path = get_test_data_dir()
    return await readjsonfile(path, 'coll_user.json')


@pytestmark
async def get_config():
    path = get_test_data_dir()
    return await readjsonfile(path, 'config.json')


@pytestmark
async def init_main_collections(env: OzonEnv):
    path = get_base_data_dir()
    await init_collecetion(env, path, 'components.json', "component")


@pytestmark
async def get_base_data(filename):
    path = get_base_data_dir()
    return await readjsonfile(path, f'{filename}.json')


async def get_base_data_record(filename, rec_name) -> Dict:
    path = get_base_data_dir()
    data = await readjsonfile(path, f'{filename}.json')
    for item in data:
        if item['rec_name'] == rec_name:
            return item


@pytestmark
async def downlad_file(self, file_url):
    return await readfile(self.upload_folder, file_url)
