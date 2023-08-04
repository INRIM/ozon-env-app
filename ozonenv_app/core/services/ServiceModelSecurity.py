# Copyright INRIM (https://www.inrim.eu)
# See LICENSE file for full licensing details.
import logging
import uuid
from ozonenv_app.core.BaseModel import BaseModel
from ozonenv_app.core.OzonEnvApp import OzonEnvApp
from ozonenv_app.core.OzonModelApp import OzonModelApp
from ozonenv.core.BaseModels import CoreModel, Session

logger = logging.getLogger(__name__)


class ServiceModelSecurity:
    def __init__(self, app: OzonEnvApp):
        self.app: OzonEnvApp = app
        self.env = self.app.env
        self.session: Session = self.app.session
        self.client = None

    # il modello ACL e' collogato al singolo componente
    # nel caso un model sia figlio di un altro model
    # le policy rules vengono copiate dal parent e poi possono essere modificate
    # si po' ad esempio dare un accesso ad un form di un progetto con restrizioni
    # ma lo si potra' raggiungere solo con un link diretto.
    async def check_action_app_code(self, model: CoreModel) -> bool:
        # if is not admin app, check access action by app code
        if not model.app_code:
            return True
        if self.app_code not in model.app_code:
            return False
        return True

    # TODO imp load schema and eval from rule model
    async def can_create(
        self, model: OzonModelApp, data: CoreModel, action=None
    ):
        logger.debug(
            f"ACL can_create {self.session.user.get('uid')} -> "
            f"{data.owner_uid} | user Admin {self.session.is_admin}"
        )
        create = await self.check_action_app_code(model.model)

        if not create:
            return create

        if (
            data.owner_uid == self.session.user.get('uid')
            or self.session.user_function == "resp"
        ):
            create = True

        if self.session.is_admin:
            create = True

        logger.debug(
            f"ACL can_create {self.session.user.get('uid')} ->  {create}"
        )
        return create

    async def can_read(self, data: CoreModel):
        logger.info(
            f"ACL can_read {self.session.user.get('uid')}, "
            f"user Admin {self.session.is_admin}, model {data.rec_name}"
        )
        # readable = True

        # if is not admin app, check access action by app code
        readable = await self.check_action_app_code(data)

        logger.debug(
            f"ACL can_read {self.session.user.get('uid')} ->  {readable}"
        )
        return readable

    async def can_update(
        self, model: OzonModelApp, data: CoreModel, action: CoreModel = None
    ):
        logger.debug(
            f"ACL can_update req user: {self.session.user.get('uid')} -> "
            f"data owner: {data.owner_uid}, "
            f"req user Admin: {self.session.is_admin}"
        )

        if not data:
            return True

        editable = await self.check_action_app_code(model.model)
        if not editable:
            return editable

        if data.owner_uid == self.session.user.get('uid') or (
            self.session.function == "resp"
            and data.owner_sector_id == self.session.sector_id
        ):
            editable = True

        if self.session.is_admin:
            editable = True

        logger.debug(
            f"ACL can_edit {self.session.user.get('uid')} ->  {editable}"
        )
        return editable

    async def can_update_fields(
        self, model: CoreModel, data: CoreModel, action: CoreModel = None
    ):
        logger.debug(f"ACL Fields")
        fields = []
        logger.info(
            f"ACL editable_fields {self.session.user.get('uid')} ->  {fields}"
        )
        return fields

    async def can_delete(
        self, model: OzonModelApp, data: CoreModel, action: CoreModel = None
    ):
        logger.debug(
            f"ACL can_delete {self.session.user.get('uid')} -> "
            f"{data.owner_uid} | "
            f"user Admin {self.session.is_admin}"
        )

        editable = await self.check_action_app_code(model.model)
        if not editable:
            return editable

        if (
            model.owner_uid == self.session.user.get('uid')
            or self.session.user_function == "resp"
        ):
            editable = True

        if self.session.is_admin:
            return True

        logger.debug(
            f"ACL can_delete {self.session.user.get('uid')} ->  {editable}"
        )
        return editable

    async def make_user_action_query(self):
        logger.debug(
            f"ACL user_action_query {self.session.user.get('uid')}  | "
            f"user Admin {self.session.is_admin}"
        )
        query_list = []
        user = self.session.user
        if self.session.is_admin:
            return []

        function = user.get('user_function')
        query_list.append({"admin": False, "sys": False})
        if function == "resp":
            query_list.append(
                {"user_function": {"$elemMatch": {"$eq": ['user', 'resp']}}}
            )
        else:
            query_list.append({"user_function": "user"})
        if self.session.is_public:
            query_list.append({"no_public_user": False})

        return query_list
