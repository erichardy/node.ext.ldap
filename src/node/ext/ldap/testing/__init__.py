import os
import shutil
import subprocess
import tempfile
import time
import logging
from odict import odict
from plone.testing import Layer, zca
from pkg_resources import resource_filename
from node.ext.ldap import (
    ONELEVEL,
    SUBTREE,
    LDAPProps,
    )
from node.ext.ldap.ugm import (
    UsersConfig,
    GroupsConfig,
    )


SCHEMA = os.environ.get('SCHEMA')
try:
    SLAPDBIN = os.environ['SLAPD_BIN']
    SLAPDURIS = os.environ['SLAPD_URIS']
    LDAPADDBIN = os.environ['LDAP_ADD_BIN']
    LDAPDELETEBIN = os.environ['LDAP_DELETE_BIN']
    LDAPSUFFIX = os.environ.get('LDAP_SUFFIX', None) or "dc=my-domain,dc=com"
except KeyError, e:                                          #pragma NO COVERAGE
    logging.exception(                                       #pragma NO COVERAGE
        u"Environment variables SLAPD_BIN, SLAPD_URIS, "     #pragma NO COVERAGE
        u"LDAP_ADD_BIN, LDAP_DELETE_BIN needed.")            #pragma NO COVERAGE 
    exit(1)                                                  #pragma NO COVERAGE

def resource(string):
    return resource_filename(__name__, string)


def read_env(layer):
    if layer.get('confdir', None) is not None:
        return
    tmpdir = os.environ.get('node.ext.ldap.testldap.env', None)
    if tmpdir is None:
        tmpdir = tempfile.mkdtemp()
        layer['externalpidfile'] = False
    else:
        # case testldap server started via node.ext.ldap.main.slapd
        layer['externalpidfile'] = True                     #pragma NO COVERAGE
    layer['confdir'] = tmpdir
    layer['dbdir'] = "%s/openldap-data" % (layer['confdir'],)
    layer['slapdconf'] = "%s/slapd.conf" % (layer['confdir'],)
    layer['binddn'] = "cn=Manager,%s" % LDAPSUFFIX
    layer['suffix'] = LDAPSUFFIX
    layer['bindpw'] = "secret"
    print tmpdir


slapdconf_template = """\
%(schema)s

pidfile		%(confdir)s/slapd.pid
argsfile	%(confdir)s/slapd.args

database	bdb
suffix		"%(suffix)s"
rootdn		"%(binddn)s"
rootpw		%(bindpw)s
directory	%(dbdir)s
# Indices to maintain
index	objectClass	eq

overlay memberof
"""


class SlapdConf(Layer):
    """generate slapd.conf
    """
    
    def __init__(self, schema):
        """
        ``schema``: List of paths to our schema files
        """
        super(SlapdConf, self).__init__()
        self.schema = schema

    def setUp(self, args=None):
        """take a template, replace, write slapd.conf store path for others to
        knows
        """
        read_env(self)
        suffix = self['suffix']
        binddn = self['binddn']
        bindpw = self['bindpw']
        confdir = self['confdir']
        dbdir = self['dbdir']
        slapdconf = self['slapdconf']
        schema = '\n'.join(
            ["include %s" % (schema,) for schema in self.schema]
            )
        # generate config file
        with open(slapdconf, 'w') as slapdconf:
            slapdconf.write(slapdconf_template % dict(
                    binddn=binddn,
                    bindpw=bindpw,
                    confdir=confdir,
                    dbdir=dbdir,
                    schema=schema,
                    suffix=suffix,
                    ))
        os.mkdir(dbdir)
        self['props'] = props
        print "SlapdConf set up."

    def tearDown(self):
        """remove our traces
        """
        read_env(self)
        shutil.rmtree(self['confdir'])
        print "SlapdConf torn down."


schema = [
    resource('schema/core.schema'),
    resource('schema/cosine.schema'),
    resource('schema/inetorgperson.schema'),
    resource('schema/nis.schema'),
    resource('schema/samba.schema'),
    resource('schema/ad.dummy.schema'),
]

SLAPD_CONF = SlapdConf(schema)


class LDAPLayer(Layer):
    """Base class for ldap layers to _subclass_ from.
    """
    defaultBases = (SLAPD_CONF,)

    def __init__(self, uris=SLAPDURIS, **kws):
        super(LDAPLayer, self).__init__(**kws)
        self['uris'] = uris


class Slapd(LDAPLayer):
    """Start/Stop an LDAP Server.
    """
    
    def __init__(self, slapdbin=SLAPDBIN, **kws):
        super(Slapd, self).__init__(**kws)
        self.slapdbin = slapdbin
        self.slapd = None

    def setUp(self, args=['-d', '0']):
        """start slapd
        """
        print "\nStarting LDAP server: ",
        read_env(self)
        cmd = [self.slapdbin, '-f', self['slapdconf'], '-h', self['uris']]
        cmd += args
        print ' '.join(cmd)
        self.slapd = subprocess.Popen(cmd)
        time.sleep(1)
        print "done."

    def tearDown(self):
        """stop the previously started slapd
        """
        print "\nStopping LDAP Server: ",
        read_env(self)
        if self['externalpidfile']:
            # case testldap server started via node.ext.ldap.main.slapd
            path = os.path.join(                           #pragma NO COVERAGE
                self['confdir'], 'slapd.pid')              #pragma NO COVERAGE
            with open(path) as pidfile:                    #pragma NO COVERAGE
                pid = int(pidfile.read())                  #pragma NO COVERAGE
        else:
            pid = self.slapd.pid
        os.kill(pid, 15)
        if self.slapd is not None:
            print "waiting for slapd to terminate...",
            self.slapd.wait()
        print "done."
        print "Whiping ldap data directory %s: " % (self['dbdir'],),
        for file in os.listdir(self['dbdir']):
            os.remove('%s/%s' % (self['dbdir'], file))
        print "done."


SLAPD = Slapd()


class Ldif(LDAPLayer):
    """Adds/removes ldif data to/from a server
    """
    defaultBases = (SLAPD,)

    def __init__(self,
                 ldifs=tuple(),
                 ldapaddbin=LDAPADDBIN,
                 ldapdeletebin=LDAPDELETEBIN,
                 ucfg=None,
                 gcfg=None,
                 **kws):
        super(Ldif, self).__init__(**kws)
        self.ldapaddbin = ldapaddbin
        self.ldapdeletebin = ldapdeletebin
        self.ldifs = type(ldifs) is tuple and ldifs or (ldifs,)
        self.ucfg = ucfg
        self.gcfg = gcfg

    def setUp(self):
        """run ldapadd for list of ldifs
        """
        
        # Create a new global registry
        #if not os.environ.get('node.ext.ldap.testldap.skip_zca_hook'):
        #    import zope.testing.cleanup
        #    zope.testing.cleanup.cleanUp()            
        #    zca.pushGlobalRegistry()
        
        if not os.environ.get('node.ext.ldap.testldap.skip_zca_hook'):
            zca.pushGlobalRegistry()

        read_env(self)
        self['ucfg'] = self.ucfg
        self['gcfg'] = self.gcfg
        print
        for ldif in self.ldifs:
            print "Adding ldif %s: " % (ldif,),
            cmd = [self.ldapaddbin, '-f', ldif, '-x', '-D', self['binddn'], '-w',
                   self['bindpw'], '-c', '-a', '-H', self['uris']]
            retcode = subprocess.call(cmd)
            print "done. %s" % retcode

    def tearDown(self):
        """remove previously added ldifs
        """
        read_env(self)
        for ldif in self.ldifs:
            print "Removing ldif %s recursively: " % (ldif,),
            with open(ldif) as ldif:
                dns = [x.strip().split(' ',1)[1]  for x in ldif if
                       x.startswith('dn: ')]
            cmd = [self.ldapdeletebin, '-x', '-D', self['binddn'], '-c', '-r',
                   '-w', self['bindpw'], '-H', self['uris']] + dns
            retcode = subprocess.call(cmd, stderr=subprocess.PIPE)
            print "done. %s" % retcode
        for key in ('ucfg', 'gcfg'):
            if key in self:
                del self[key]
       
        # Pop the global registry
        #if not os.environ.get('node.ext.ldap.testldap.skip_zca_hook'):
        #    import zope.testing.cleanup
        #    zope.testing.cleanup.cleanUp()  
        
        if not os.environ.get('node.ext.ldap.testldap.skip_zca_hook'):
            zca.popGlobalRegistry()         


ldif_layer = odict()
            
# testing ldap props
user = 'cn=Manager,dc=my-domain,dc=com'
pwd = 'secret'


props = LDAPProps(
    uri='ldap://127.0.0.1:12345/',
    user=user,
    password=pwd,
    cache=False)


# base users config
ucfg = UsersConfig(
    baseDN='dc=my-domain,dc=com',
    attrmap={
        'id': 'sn',
        'login': 'description',
        'telephoneNumber': 'telephoneNumber',
        'rdn': 'ou',
        'sn': 'sn',
        },
    scope=SUBTREE,
    queryFilter='(objectClass=person)',
    objectClasses=['person'])


# inetOrgPerson r/w attrs
inetOrgPerson_attrmap = {
    'id': 'uid',
    'login': 'uid',
    'cn': 'cn',
    'rdn': 'uid',
    'sn': 'sn',
    'mail': 'mail',
},


# users config for 300-users data.
ucfg300 = UsersConfig(
    baseDN='ou=users300,dc=my-domain,dc=com',
    attrmap=inetOrgPerson_attrmap,
    scope=ONELEVEL,
    queryFilter='(objectClass=inetOrgPerson)',
    objectClasses=['inetOrgPerson'])


# users config for 700-users data.
ucfg700 = UsersConfig(
    baseDN='ou=users700,dc=my-domain,dc=com',
    attrmap=inetOrgPerson_attrmap,
    scope=ONELEVEL,
    queryFilter='(objectClass=inetOrgPerson)',
    objectClasses=['inetOrgPerson'])


# users config for 1000-users data.
ucfg1000 = UsersConfig(
    baseDN='ou=users1000,dc=my-domain,dc=com',
    attrmap=inetOrgPerson_attrmap,
    scope=ONELEVEL,
    queryFilter='(objectClass=inetOrgPerson)',
    objectClasses=['inetOrgPerson'])


# users config for 2000-users data.
ucfg2000 = UsersConfig(
    baseDN='ou=users2000,dc=my-domain,dc=com',
    attrmap=inetOrgPerson_attrmap,
    scope=ONELEVEL,
    queryFilter='(objectClass=inetOrgPerson)',
    objectClasses=['inetOrgPerson'])


# base groups config
#gcfg_openldap = GroupsConfig(
#        baseDN='dc=my-domain,dc=com',
#        id_attr='cn',
#        scope=SUBTREE,
#        queryFilter='(objectClass=groupOfNames)',
#        member_relation='member:dn',
#        )


LDIF_data = Ldif(
    resource('ldifs/data.ldif'),
    name='LDIF_data',
    ucfg=ucfg)
ldif_layer['data'] = LDIF_data


LDIF_principals = Ldif(
    resource('ldifs/principals.ldif'),
    bases=(LDIF_data,),
    name='LDIF_principals',
    ucfg=ucfg)
ldif_layer['principals'] = LDIF_principals


LDIF_data_old_props = Ldif(
    resource('ldifs/data.ldif'),
    name='LDIF_data',
    ucfg=ucfg)
ldif_layer['data_old_props'] = LDIF_data_old_props


LDIF_principals_old_props = Ldif(
    resource('ldifs/principals.ldif'),
    bases=(LDIF_data,),
    name='LDIF_principals',
    ucfg=ucfg)
ldif_layer['principals_old_props'] = LDIF_principals_old_props


# new ones
LDIF_base = Ldif(
    resource('ldifs/base.ldif'),
    name="LDIF_base")
ldif_layer['base'] = LDIF_base


LDIF_users300 = Ldif(
    resource('ldifs/users300.ldif'),
    bases=(LDIF_base,),
    name="LDIF_users300",
    ucfg=ucfg300)
ldif_layer['users300'] = LDIF_users300


LDIF_users700 = Ldif(
    resource('ldifs/users700.ldif'),
    bases=(LDIF_base,),
    name="LDIF_users700",
    ucfg=ucfg700)
ldif_layer['users700'] = LDIF_users700


LDIF_users1000 = Ldif(
    resource('ldifs/users1000.ldif'),
    bases=(LDIF_base,),
    name="LDIF_users1000",
    ucfg=ucfg1000)
ldif_layer['users1000'] = LDIF_users1000


LDIF_users2000 = Ldif(
    resource('ldifs/users2000.ldif'),
    bases=(LDIF_base,),
    name="LDIF_users2000",
    ucfg=ucfg2000)
ldif_layer['users2000'] = LDIF_users2000


# Users and groups

groupOfNamesUcfgAttrmap = {
    'id': 'uid',
    'login': 'cn',
    'rdn': 'uid',
    'cn': 'cn',
    'sn': 'sn',
    'mail': 'mail',
}


groupOfNamesGcfgAttrmap = {
    'id': 'cn',
    'rdn': 'cn',
    'businessCategory': 'businessCategory',
}


LDIF_groupOfNames = Ldif(
    resource('ldifs/groupOfNames.ldif'),
    bases=(LDIF_base,),
    name="LDIF_groupOfNames",
    ucfg=UsersConfig(
        baseDN='ou=users,ou=groupOfNames,dc=my-domain,dc=com',
        attrmap=groupOfNamesUcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=inetOrgPerson)',
        objectClasses=['person', 'inetOrgPerson'],
        ),
    gcfg=GroupsConfig(
        baseDN='ou=groups,ou=groupOfNames,dc=my-domain,dc=com',
        attrmap=groupOfNamesGcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=groupOfNames)',
        objectClasses=['groupOfNames'],
        ),
    )
ldif_layer['groupOfNames'] = LDIF_groupOfNames


LDIF_groupOfNames_add = Ldif(
    resource('ldifs/groupOfNames_add.ldif'),
    bases=(LDIF_groupOfNames,),
    name="LDIF_groupOfNames_add",
    ucfg=UsersConfig(
        baseDN='ou=users,ou=groupOfNames,dc=my-domain,dc=com',
        attrmap=groupOfNamesUcfgAttrmap,
        scope=SUBTREE,
        queryFilter='(objectClass=inetOrgPerson)',
        objectClasses=['person', 'inetOrgPerson'],
        ),
    gcfg=GroupsConfig(
        baseDN='ou=groups,ou=groupOfNames,dc=my-domain,dc=com',
        attrmap=groupOfNamesGcfgAttrmap,
        scope=SUBTREE,
        queryFilter='(objectClass=groupOfNames)',
        objectClasses=['groupOfNames'],
        ),
    )
ldif_layer['groupOfNames_add'] = LDIF_groupOfNames_add


LDIF_groupOfNames_10_10 = Ldif(
    resource('ldifs/groupOfNames_10_10.ldif'),
    bases=(LDIF_base,),
    name="LDIF_groupOfNames_10_10",
    ucfg=UsersConfig(
        baseDN='ou=users,ou=groupOfNames_10_10,dc=my-domain,dc=com',
        attrmap=groupOfNamesUcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=inetOrgPerson)',
        objectClasses=['inetOrgPerson'],
        ),
    gcfg=GroupsConfig(
        baseDN='ou=groups,ou=groupOfNames_10_10,dc=my-domain,dc=com',
        attrmap=groupOfNamesGcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=groupOfNames)',
        objectClasses=['groupOfNames'],
        ),
    )
ldif_layer['groupOfNames_10_10'] = LDIF_groupOfNames_10_10


LDIF_groupOfNames_100_100 = Ldif(
    resource('ldifs/groupOfNames_100_100.ldif'),
    bases=(LDIF_base,),
    name="LDIF_groupOfNames_100_100",
    ucfg=UsersConfig(
        baseDN='ou=users,ou=groupOfNames_100_100,dc=my-domain,dc=com',
        attrmap=groupOfNamesUcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=inetOrgPerson)',
        objectClasses=['person', 'inetOrgPerson'],
        ),
    gcfg=GroupsConfig(
        baseDN='ou=groups,ou=groupOfNames_100_100,dc=my-domain,dc=com',
        attrmap=groupOfNamesGcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=groupOfNames)',
        objectClasses=['groupOfNames'],
        ),
    )
ldif_layer['groupOfNames_100_100'] = LDIF_groupOfNames_100_100


LDIF_groupOfNames_100_100_add = Ldif(
    resource('ldifs/groupOfNames_100_100_add.ldif'),
    bases=(LDIF_groupOfNames_100_100,),
    name="LDIF_groupOfNames_100_100_add",
    ucfg=UsersConfig(
        baseDN='ou=users,ou=groupOfNames_100_100,dc=my-domain,dc=com',
        attrmap=groupOfNamesUcfgAttrmap,
        scope=SUBTREE,
        queryFilter='(objectClass=inetOrgPerson)',
        objectClasses=['person', 'inetOrgPerson'],
        ),
    gcfg=GroupsConfig(
        baseDN='ou=groups,ou=groupOfNames_100_100,dc=my-domain,dc=com',
        attrmap=groupOfNamesGcfgAttrmap,
        scope=SUBTREE,
        queryFilter='(objectClass=groupOfNames)',
        objectClasses=['groupOfNames'],
        ),
    )
ldif_layer['groupOfNames_100_100_add'] = LDIF_groupOfNames_100_100_add


LDIF_groupOfNames_300_300 = Ldif(
    resource('ldifs/groupOfNames_300_300.ldif'),
    bases=(LDIF_base,),
    name="LDIF_groupOfNames_300_300",
    ucfg=UsersConfig(
        baseDN='ou=users,ou=groupOfNames_300_300,dc=my-domain,dc=com',
        attrmap=groupOfNamesUcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=inetOrgPerson)',
        objectClasses=['person', 'inetOrgPerson'],
        ),
    gcfg=GroupsConfig(
        baseDN='ou=groups,ou=groupOfNames_300_300,dc=my-domain,dc=com',
        attrmap=groupOfNamesGcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=groupOfNames)',
        objectClasses=['groupOfNames'],
        ),
    )
ldif_layer['groupOfNames_300_300'] = LDIF_groupOfNames_300_300


LDIF_groupOfNames_300_300_add = Ldif(
    resource('ldifs/groupOfNames_300_300_add.ldif'),
    bases=(LDIF_groupOfNames_300_300,),
    name="LDIF_groupOfNames_300_300_add",
    ucfg=UsersConfig(
        baseDN='ou=users,ou=groupOfNames_300_300,dc=my-domain,dc=com',
        attrmap=groupOfNamesUcfgAttrmap,
        scope=SUBTREE,
        queryFilter='(objectClass=inetOrgPerson)',
        objectClasses=['person', 'inetOrgPerson'],
        ),
    gcfg=GroupsConfig(
        baseDN='ou=groups,ou=groupOfNames_300_300,dc=my-domain,dc=com',
        attrmap=groupOfNamesGcfgAttrmap,
        scope=SUBTREE,
        queryFilter='(objectClass=groupOfNames)',
        objectClasses=['groupOfNames'],
        ),
    )
ldif_layer['groupOfNames_300_300_add'] = LDIF_groupOfNames_300_300_add


LDIF_groupOfNames_700_700 = Ldif(
    resource('ldifs/groupOfNames_700_700.ldif'),
    bases=(LDIF_base,),
    name="LDIF_groupOfNames_700_700",
    ucfg=UsersConfig(
        baseDN='ou=users,ou=groupOfNames_700_700,dc=my-domain,dc=com',
        attrmap=groupOfNamesUcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=inetOrgPerson)',
        objectClasses=['person', 'inetOrgPerson'],
        ),
    gcfg=GroupsConfig(
        baseDN='ou=groups,ou=groupOfNames_700_700,dc=my-domain,dc=com',
        attrmap=groupOfNamesGcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=groupOfNames)',
        objectClasses=['groupOfNames'],
        ),
    )
ldif_layer['groupOfNames_700_700'] = LDIF_groupOfNames_700_700


LDIF_groupOfNames_700_700_add = Ldif(
    resource('ldifs/groupOfNames_700_700_add.ldif'),
    bases=(LDIF_groupOfNames_700_700,),
    name="LDIF_groupOfNames_700_700_add",
    ucfg=UsersConfig(
        baseDN='ou=users,ou=groupOfNames_700_700,dc=my-domain,dc=com',
        attrmap=groupOfNamesUcfgAttrmap,
        scope=SUBTREE,
        queryFilter='(objectClass=inetOrgPerson)',
        objectClasses=['person', 'inetOrgPerson'],
        ),
    gcfg=GroupsConfig(
        baseDN='ou=groups,ou=groupOfNames_700_700,dc=my-domain,dc=com',
        attrmap=groupOfNamesGcfgAttrmap,
        scope=SUBTREE,
        queryFilter='(objectClass=groupOfNames)',
        objectClasses=['groupOfNames'],
        ),
    )
ldif_layer['groupOfNames_700_700_add'] = LDIF_groupOfNames_700_700_add


LDIF_groupOfNames_1000_1000 = Ldif(
    resource('ldifs/groupOfNames_1000_1000.ldif'),
    bases=(LDIF_base,),
    name="LDIF_groupOfNames_1000_1000",
    ucfg=UsersConfig(
        baseDN='ou=users,ou=groupOfNames_1000_1000,dc=my-domain,dc=com',
        attrmap=groupOfNamesUcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=inetOrgPerson)',
        objectClasses=['person', 'inetOrgPerson'],
        ),
    gcfg=GroupsConfig(
        baseDN='ou=groups,ou=groupOfNames_1000_1000,dc=my-domain,dc=com',
        attrmap=groupOfNamesGcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=groupOfNames)',
        objectClasses=['groupOfNames'],
        ),
    )
ldif_layer['groupOfNames_1000_1000'] = LDIF_groupOfNames_1000_1000


LDIF_groupOfNames_1000_1000_add = Ldif(
    resource('ldifs/groupOfNames_1000_1000_add.ldif'),
    bases=(LDIF_groupOfNames_1000_1000,),
    name="LDIF_groupOfNames_1000_1000_add",
    ucfg=UsersConfig(
        baseDN='ou=users,ou=groupOfNames_1000_1000,dc=my-domain,dc=com',
        attrmap=groupOfNamesUcfgAttrmap,
        scope=SUBTREE,
        queryFilter='(objectClass=inetOrgPerson)',
        objectClasses=['person', 'inetOrgPerson'],
        ),
    gcfg=GroupsConfig(
        baseDN='ou=groups,ou=groupOfNames_1000_1000,dc=my-domain,dc=com',
        attrmap=groupOfNamesGcfgAttrmap,
        scope=SUBTREE,
        queryFilter='(objectClass=groupOfNames)',
        objectClasses=['groupOfNames'],
        ),
    )
ldif_layer['groupOfNames_1000_1000_add'] = LDIF_groupOfNames_1000_1000_add


# Users and groups (posix)

posixGroupsUcfgAttrmap = {
    'id': 'uid',
    'login': 'cn',
    'rdn': 'uid',
    'cn': 'cn',
    'uidNumber': 'uidNumber',
    'gidNumber': 'gidNumber',
    'homeDirectory': 'homeDirectory',
}


posixGroupsGcfgAttrmap = {
    'id': 'cn',
    'rdn': 'cn',
    'gidNumber': 'gidNumber',
}


LDIF_posixGroups = Ldif(
    resource('ldifs/posixGroups.ldif'),
    bases=(LDIF_base,),
    name="LDIF_posixGroups",
    ucfg=UsersConfig(
        baseDN='ou=users,ou=posixGroups,dc=my-domain,dc=com',
        attrmap=posixGroupsUcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=posixAccount)',
        objectClasses=['account', 'posixAccount'],
        ),
    gcfg=GroupsConfig(
        baseDN='ou=groups,ou=posixGroups,dc=my-domain,dc=com',
        attrmap=posixGroupsGcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=posixGroup)',
        objectClasses=['posixGroup'],
        ),
    )
ldif_layer['posixGroups'] = LDIF_posixGroups


# users (samba)

sambaUsersUcfgAttrmap = {
    'id': 'uid',
    'login': 'uid',
    'rdn': 'uid',
    'sambaSID': 'sambaSID',
    'sambaNTPassword': 'sambaNTPassword',
    'sambaLMPassword': 'sambaLMPassword',
}


LDIF_sambaUsers = Ldif(
    resource('ldifs/sambaUsers.ldif'),
    bases=(LDIF_base,),
    name='LDIF_sambaUsers',
    ucfg=UsersConfig(
        baseDN='ou=sambaUsers,dc=my-domain,dc=com',
        attrmap=sambaUsersUcfgAttrmap,
        scope=ONELEVEL,
        queryFilter='(objectClass=sambaSamAccount)',
        objectClasses=['sambaSamAccount'],
        ),
    )
ldif_layer['sambaUsers'] = LDIF_sambaUsers
