from httpx import AsyncClient

from test_common import *


@pytestmark
async def test_3_test_api1():
    async with AsyncClient(base_url="http://localhost:8000") as client:
        response = await client.get("/")
    assert response.status_code == 307



@pytestmark
async def test_3_test_api2():
    async with AsyncClient(base_url="http://localhost:8000") as client:
        response = await client.get("/login")
    assert response.status_code == 200
