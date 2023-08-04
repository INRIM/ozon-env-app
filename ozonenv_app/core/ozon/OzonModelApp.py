from typing import Any, List, Dict

from ozonenv.core.BaseModels import (
    BasicModel,
    default_list_metadata_fields,
    default_list_metadata,
)
# from typing import Annotated
from ozonenv.core.BaseModels import CoreModel
from ozonenv.core.OzonOrm import OzonModel, OzonOrm
from ozonenv.core.i18n import _

from BaseModel import ResponseDataErr
from ozonenv_app.core.services.QueryEngine import QueryEngine


class OzonModelApp(OzonModel):
    def __init__(
            self,
            model_name,
            orm: OzonOrm,
            data_model="",
            session_model=False,
            virtual=False,
            static: CoreModel = None,
            schema: dict = None,
    ):
        super().__init__(
            model_name,
            orm,
            data_model=data_model,
            session_model=session_model,
            virtual=virtual,
            static=static,
            schema=schema,
        )
        if schema is None:
            schema = {}
        self.qe: QueryEngine = None
        self.ignore_set_user_data = False

    def init_engine(self):
        self.qe = QueryEngine(
            self.orm.user_session, self.orm.app_settings, self.orm.app_code
        )

    def get_schema_fields(self) -> (Dict[str, Any], [str]):
        schema = self.model.model_dump()
        model_fields_names = [k for k, v in schema['properties'].items()]
        fields = [
            item
            for item in model_fields_names
            if item not in default_list_metadata_fields
        ]
        if "data_value" in fields:
            fields.remove("data_value")
        return schema, fields

    # update multi records
    async def update_all(
            self, records: list[BasicModel], remove_meta=False
    ) -> list[CoreModel]:
        res = []
        for record in records:
            res.append(await self.update(record, remove_mata=remove_meta))
        return res



    # get by recname
    async def by_name(self, name: str) -> CoreModel:
        return await self.load({'rec_name': name})

    async def search_base(
            self,
            query: dict = None,
            sort: str = "",
            limit: int = 0,
            skip: int = 0,
            pipeline_items:list=None,
    ) -> List[CoreModel]:
        """
        search data in record
        """
        if pipeline_items is None:
            pipeline_items = []
        self.init_status()
        if query is None:
            query = {}
        if pipeline_items:
            list_data = await self.aggregate(pipeline_items, sort=sort)
        else:
            list_data = await self.find(
                query, sort=sort, limit=limit, skip=skip
            )

        return list_data

    async def search(
            self,
            fields: list = None,
            query: dict = None,
            sort: str = "",
            limit: int = 0,
            skip: int = 0,
            row_action="",
            additional_pipeline_oper: List[Dict] = [],
    ) -> List[CoreModel]:
        if query is None:
            query = {}
        if fields is None:
            fields = []
        if fields:
            fields = fields + default_list_metadata

        pipeline_items = [
            {"$match": query},
        ]
        if fields:
            ...

        add_feild = {"$addFields": {}}

        if row_action:
            add_feild["$addFields"]["row_action"] = {
                "$concat": [row_action, "/", "$rec_name"]
            }

        pipeline_items.append(add_feild)

        list_data = await self.search_base(
            query=query,
            sort=sort,
            limit=limit,
            skip=skip,
            pipeline_items=pipeline_items,
        )
        return list_data

    async def search_export(
            self,
            fields: list = None,
            query: dict = None,
            sort: str = "",
            limit: int = 0,
            skip: int = 0,
    ) -> List[CoreModel]:
        if fields is None:
            fields = []
        if query is None:
            query = {}

        pipeline_items = [
            {"$match": query},
        ]

        list_data = await self.search_base(
            query=query,
            sort=sort,
            limit=limit,
            skip=skip,
            pipeline_items=pipeline_items,
        )

        return list_data

    async def default_query(
            self, query=None, parent="", model_type=""
    ) -> dict:
        if query is None:
            query = {}
        if self.virtual and not self.data_model:
            self.error_status(
                _(
                    f"Cannot create query on db for "
                    f"virtual object with model {self.data_model}"
                ),
                self.data_model,
            )
            return {}

        return await self.qe.default_query(
            self.data_model, query=query, parent=parent, model_type=model_type
        )

    async def search_count_field_value_freq(
            self,
            field: str = "",
            field_query: dict = None,
            min_occurence: int = 2,
            add_fields: str = "",
            sort: int = -1,
    ) -> List[CoreModel]:
        if field_query is None:
            field_query = {}
        self.init_status()
        logger.debug("search_all_distinct")
        group = {"_id": f'${field}', "count": {"$sum": 1}}
        if add_fields:
            label_lst = add_fields.split(",")
            for item in label_lst:
                group.update({f"$item": {"$first": item}})

        query = {"$and": [{"deleted": 0}, field_query]}
        pipeline = [
            {"$match": query},
            {"$group": group},
            {"$match": {"count": {"$gte": min_occurence}}},
            {'$sort': {'count': sort}},
        ]
        return await self.aggregate(pipeline)

    def response_err(self) -> ResponseDataErr:
        return ResponseDataErr(
            message=self.status.msg,
            model=self.model.data_model,
            rec_name=self.model.rec_name,
        )

    def handle_return(self, record: CoreModel) -> Any:
        if self.status.fail:
            self.response_err()
        else:
            return record

    def set_user_data(self, record: CoreModel, user=None) -> CoreModel:
        if user is None:
            user = {}
        if not self.ignore_set_user_data:
            return super().set_user_data(record=record, user=user)
        else:
            return record
