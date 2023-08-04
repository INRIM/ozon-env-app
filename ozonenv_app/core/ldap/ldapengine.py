import ldap


class LdapEngine:
    def __init__(self):
        self.conn = None

    # Python code t get difference of two lists
    # Not using set()
    def list_diff(self, li1, li2):
        li_dif = [i for i in li1 + li2 if i not in li1 or i not in li2]
        return li_dif

    def connectAndExecute(self, servervicecfg):
        res = self.connect(servervicecfg)
        if res.get('error'):
            return res
        servervicecfg['service']['pass'] = 'secret :-)'
        return self.computeOperations(servervicecfg)

    def connect(self, servervicecfg):
        ldap.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)
        try:
            scfg = servervicecfg.get('service')
            servervicecfg['service'] = scfg
            url = scfg.get('url')
            user = scfg['user']
            password = scfg['pass']
            self.baseDn = "{}".format(servervicecfg['base_dn'])
            self.stringDn = self.baseDn
            if servervicecfg.get('uid'):
                self.stringDn = "uid={},{}".format(
                    servervicecfg['uid'], self.baseDn
                )
            self.conn = ldap.initialize(url)
            self.conn.simple_bind_s(user, password)
        except ldap.INVALID_CREDENTIALS:
            return {"error": 'Auth error'}
        except ldap.SERVER_DOWN:
            return {"error": ('An LDAP exception occurred: server down')}
        except ldap.LDAPError as e:
            if self.conn:
                self.conn.unbind()
            return {"error": ('An LDAP exception occurred: %s' % e)}
        return {"success": 'connection'}

    def computeOperations(self, servervicecfg):
        self.listOperations = []
        search_res = None
        search_res_dict = {}
        search_filter = False
        retdict = {"success": 'computeOperations'}
        delete = servervicecfg.get('delete')
        update = servervicecfg.get('update')
        create = servervicecfg.get('create')
        search = servervicecfg.get('search')
        if delete:
            self.rmOper(delete)

        if create:
            self.addOper(create)
        if update:
            self.updOper(update)
        if search:
            search_filter = search.get('search_filter', False)

        try:
            if create:
                self.conn.add_s(self.stringDn, self.listOperations)
            if update:
                self.conn.modify_s(self.stringDn, self.listOperations)
            if delete:
                for i in self.listOperations:
                    stringDn = "uid={},{}".format(
                        i, self.baseDn
                    )
                    self.conn.delete_s(stringDn)
            if search and search_filter:
                result_s = []
                if servervicecfg.get('search_result_attr'):
                    list_attribute = servervicecfg.get('search_result_attr')
                else:
                    list_attribute = False

                search_res = self.conn.search_s(
                    self.baseDn, ldap.SCOPE_SUBTREE, search_filter)
                if list_attribute and len(search_res) > 0:
                    for item in search_res:
                        for subitem in item:
                            elm = {}
                            for attr in list_attribute:
                                if not type(subitem) == str and subitem.get(
                                        attr, False):
                                    if len(subitem.get(attr)) > 1:
                                        elm[attr] = subitem.get(attr).decode()
                                    else:
                                        elm[attr] = subitem.get(attr)[
                                            0].decode()
                            if elm:
                                result_s.append(elm)
                else:
                    result_s = search_res
                search_res_dict = {"search": result_s}

        except ldap.LDAPError as e:
            self.conn.unbind()
            return {"error": ('computeOperations:err: {}'.format(e))}
        self.conn.unbind()
        if (search_res_dict):
            retdict.update(search_res_dict)
        return retdict

    def addOper(self, row):
        if row:
            for item in row:
                if type(row[item]) is str:
                    self.listOperations.append(
                        (str(item), [str.encode(row[item])])
                    )
                if type(row[item]) is int:
                    self.listOperations.append(
                        (str(item), [str.encode(str(row[item]))])
                    )
                elif type(row[item]) is list:
                    self.listOperations.append(
                        (str(item), [str.encode(rec) for rec in row[item]])
                    )

    def updOper(self, row):
        if row:
            for item in row:
                self.listOperations.append(
                    (ldap.MOD_REPLACE, str(item), str.encode(row[item]))
                )

    def rmOper(self, row):
        print("delete")
        print(row)
        if row:
            self.listOperations = row
