import logging

from .ldapengine import *

logger = logging.getLogger(__name__)


class LdapService(LdapEngine):

    def __init__(self, name, url, base_dn, bind_dn):
        super().__init__()
        self.name = name
        self.url = url
        self.base_dn = base_dn
        self.bind_dn = bind_dn
        self.uid_key = "uid"

    def prepareConfig(self, user, passwd):
        return {
            "service": {
                "name": self.name,
                "url": self.url,
                "bind_dn": self.bind_dn,
                "user": user,
                "pass": passwd
            },
            "base_dn": self.base_dn,
            "uid": False,
            "delete": False,
            "update": False,
            "create": False,
            "search": False,
            "search_result_attr": False,
        }.copy()

    def authenticate(self, uid, password):
        user = f"uid={uid},{self.base_dn}"
        config_data = self.prepareConfig(user, password)
        config_data['uid'] = uid
        res = self.connectAndExecute(config_data)
        if 'error' in res:
            logger.error(res)
            return False
        if 'success' in res:
            return True

    def exec(self, user: str, passwd: str, key: str, values: dict, key2="",
             values2=[], uid=False) -> dict:
        config_data = self.prepareConfig(user, passwd)
        if uid:
            config_data[self.uid_key] = uid
        config_data[key] = values
        if not key2 == "":
            config_data[key2] = values2
        return self.connectAndExecute(config_data)

    def add_entry(self, user: str, passwd: str, template: dict,
                  data: dict) -> dict:
        logger.info(f"Add entry {data['uid']}")
        user_data = template
        user_data.update(**data)
        res = self.exec(user, passwd, "create", user_data,
                        uid=data[self.uid_key])
        if "error" in res:
            logger.info(f"Error")
            logger.info(f"{res}")
        logger.info(f"Add entry {data['uid']} status {res}")
        return res

    def search_list(
            self, user: str, passwd: str,
            attributes: list = ['uid'],
            filter_object_str: str = "(objectClass=inetOrgPerson)") -> dict:

        filter = {
            "search_filter": filter_object_str
        }
        res = self.exec(
            user, passwd, "search", filter, key2="search_result_attr",
            values2=attributes)
        li_all = []
        if res.get("success") and res.get('search'):
            li_all = res['search']
        else:
            logger.error("-------")
            logger.error(res)
            logger.error("-------")
        return li_all

    def check_delete_uids(self, user: str, passwd: str,
                          uid_list: list) -> dict:
        filter = {
            "search_filter": "(objectClass=inetOrgPerson)"
        }
        attributes = ["uid"]
        res = self.exec(
            user, passwd, "search", filter, key2="search_result_attr",
            values2=attributes)
        print(uid_list)
        print(res)
        if "success" in res and res.get('search'):
            li_all = [i['uid'] for i in res['search']]
            to_remove = list(set(uid_list) - set(li_all))
            to_remove = self.list_diff(uid_list, li_all)

            if to_remove:
                print("-")
                print("RM")
                print(to_remove)
                print("-")
                res = self.exec(user, passwd, "delete", to_remove)
            return res
        else:
            return res

    def connect_execute(self, data):
        res = self.connectAndExecute(data)
        return res
