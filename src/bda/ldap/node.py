# Copyright 2008-2009, BlueDynamics Alliance, Austria - http://bluedynamics.com
# GNU General Public Licence Version 2 or later

import types
import copy
from odict import odict
from zope.interface import implements
try:
    from zope.app.event.objectevent import objectEventNotify # BBB
except ImportError, e:
    from zope.component.event import objectEventNotify
from zodict.interfaces import ICallableNode
from zodict import LifecycleNode
from zodict.node import NodeAttributes
from bda.ldap import (
    BASE,
    ONELEVEL,
    LDAPSession,
)
from bda.ldap.filter import LDAPFilter, LDAPDictFilter
from bda.ldap.strcodec import encode, decode, LDAP_CHARACTER_ENCODING
from ldap.functions import explode_dn
from ldap import (
    MOD_ADD,
    MOD_DELETE,
    MOD_REPLACE,
)

ACTION_ADD = 0
ACTION_MODIFY = 1
ACTION_DELETE = 2

def queryNode(props, dn):
    """Query an ldap entry and return as LDAPNode.
    
    ``props``
        ``LDAPProps`` instance
    ``dn``
        DN of the node to query

    The difference to just LDAPNode(props=props, name=dn) is, that the node
    generated here will have only the rdn as its name, whereas LDAPNode() would
    generate a root node, with the full dn as its name.
    """
    containerdn = ','.join(explode_dn(dn)[1:])
    nodedn = explode_dn(dn)[0]
    container = LDAPNode(name=containerdn, props=props)
    return container.get(nodedn, None)

class LDAPNodeAttributes(NodeAttributes):
    
    def __init__(self, node):
        super(LDAPNodeAttributes, self).__init__(node)
        self.load()

    def load(self):
        if not self._node.__name__ or self._node._action == ACTION_ADD:
            return
        self.clear()
        # fetch our node with all attributes
        entry = self._node._session.search(
                scope=BASE,
                baseDN=self._node.DN,
                force_reload=self._node._reload
                )
        if len(entry) != 1:
            raise RuntimeError(u"Fatal. Expected entry does not exist or "
                                "more than one entry found")
        attrs = entry[0][1]
        for key, item in attrs.items():
            if len(item) == 1:
                self[key] = item[0]
            else:
                self[key] = item
        # superclass' __setitem__ set our changed flag. We just loaded from
        # ldap and are not changed.
        self.changed = False
        if self._node._action not in [ACTION_ADD, ACTION_DELETE]:
            self._node._action = None
            self._node.changed = False
                
    def __setitem__(self, key, val):
        if isinstance(key, str):
            key = decode(key)
        if isinstance(val, str):
            val = decode(val)
        super(LDAPNodeAttributes, self).__setitem__(key, val)
        self._set_attrs_modified()
    
    def __delitem__(self, key):
        if isinstance(key, str):
            key = decode(key)
        super(LDAPNodeAttributes, self).__delitem__(key)
        self._set_attrs_modified()
    
    def __getattribute__(self, name):
        # XXX: We don't support node.attrs.foo syntax yet
        return object.__getattribute__(self, name)
    
    def __setattr__(self, name, value):
        # XXX: We don't support node.attrs.foo = 1 syntax yet
        object.__setattr__(self, name, value)
    
    def _set_attrs_modified(self):
        if self._node._action not in [ACTION_ADD, ACTION_DELETE]:
            self._node._action = ACTION_MODIFY
            self._node.changed = True


class MappedAttributes(object):
    def __init__(self, node, attrmap):
        """
        ``node``
            the parent node from which to fetch the mapped attributes
        ``attrmap``
            an attribute map, eg {'key_here': 'key_in_node.attrs'}.
        """
        self._node = node
        self._map = decode(attrmap)

    def __contains__(self, key):
        return key in self._map

    def __iter__(self):
        # Just return the iterator of our keymap
        return self._map.__iter__()

    iterkeys = __iter__

    def iteritems(self):
        for key in self._map:
            yield key, self[key]

    def itervalues(self):
        for key in self._map:
            yield self[key]

    def keys(self):
        return [x for x in self._map]

    def __len__(self):
        return self._map.__len__()

    def __getitem__(self, key):
        mkey = self._map[key]
        return self._node.attrs[mkey]

    def __setitem__(self, key, val):
        mkey = self._map[key]
        self._node.attrs[mkey] = val

    def values(self):
        return [x for x in self.itervalues()]


class LDAPNode(LifecycleNode):
    """An LDAP Node.
    """
    implements(ICallableNode)
    attributes_factory = LDAPNodeAttributes
    
    def __init__(self, name=None, props=None, attrmap=None):
        """LDAP Node expects ``name`` and ``props`` arguments for the root LDAP
        Node or nothing for children. ``attrmap`` is an optional rood node
        argument.
        
        ``name`` 
            Initial base DN for the root LDAP Node.
        
        ``props`` 
            ``bda.ldap.LDAPProperties`` object.

        ``attrmap``
            an optional map of attributes, mapped attributes will be available
            via node.mattrs.
        """
        if (name and not props) or (props and not name):
            raise ValueError(u"Wrong initialization.")
        if name and not isinstance(name, unicode):
            name = name.decode(LDAP_CHARACTER_ENCODING)
        self.__name__ = name
        self.__parent__ = None
        self._session = None        
        self._changed = False
        self._action = None
        # the _keys is None or an odict.
        # if an odict, the value is either None or the value
        # None means, the value wasnt loaded 
        self._keys = None 
        self._seckeys = None
        self._seckey_attrs = None
        self._child_dns = {}
        self._reload = False        
        if props:
            self._session = LDAPSession(props)
            self._session.baseDN = self.DN
        super(LDAPNode, self).__init__(name)
        self._key_attr = 'rdn'
        self._search_scope = ONELEVEL
        self._search_filter = None
        self._search_criteria = None
        self._ChildClass = LDAPNode
        if attrmap is not None:
            self._mattrs = MappedAttributes(self, attrmap)
        else:
            self._mattrs = None
            
    @property
    def DN(self):
        if self.__parent__ is not None:
            return self.__parent__.child_dn(self.__name__)
        elif self.__name__ is not None:
            # We should not have a name if we are not a root node
            return self.__name__
        else:
            return u''

    @property
    def mattrs(self):
        if self._mattrs is None:
            raise AttributeError(u"No mapped attributes!")
        return self._mattrs

    def child_dn(self, key):
        return self._child_dns[key]

    def _calculate_key(self, dn, attrs):
        if self._key_attr == 'rdn':
            # explode_dn is ldap world
            key = decode(explode_dn(encode(dn))[0])
        else:
            key = attrs[self._key_attr]
            if isinstance(key, list):
                if len(key) != 1:
                    raise KeyError(u"Expected one value for '%s' "+
                            u"not %s: '%s'." % \
                                    (self._key_attr, len(key), key))
                key = key[0]
        return key

    def _calculate_seckeys(self, attrs):
        if not self._seckey_attrs:
            return {}
        seckeys = {}
        for seckey_attr in self._seckey_attrs:
            try:
                seckey = attrs[seckey_attr]
            except KeyError:
                raise KeyError(
                        u"Secondary key '%s' missing on: %s." % \
                                (seckey_attr, attrs['dn']))
            else:
                if isinstance(seckey, list):
                    if len(seckey) != 1:
                        raise KeyError(u"Expected one value for '%s' "+
                                u"not %s: '%s'." % \
                                        (seckey_attr, len(seckey), seckey))
                    seckey = seckey[0]
                seckeys[seckey_attr] = seckey
        return seckeys

    def search(self, queryFilter=None, criteria=None, attrlist=None,
               exact_match=False):
        """Returns a list of matching keys.

        All search criteria are additive and will be ``&``ed. ``queryFilter``
        and ``criteria`` further narrow down the search space defined by
        ``self._search_filter`` and ``self._search_criteria``.

        ``queryFilter``
            ldap queryFilter, e.g. ``(objectClass=foo)``
        ``criteria``
            dictionary of attribute value(s) (string or list of string)
        ``attrlist``
            Normally a list of keys is returned. By defining attrlist the
            return format will be ``[(key, {attr1: [value1, ...]}), ...]``. To
            get this format without any attributs, i.e. empty dicts in the
            tuples, specify an empty attrlist.
        ``exact_match``
            raise ValueError if not one match, return format is a single key or
            tuple, if attrlist is specified.
        """
        _attrlist = []
        if attrlist:
            _attrlist.extend(filter(lambda x: x != 'dn', attrlist))
        if not self._key_attr == 'rdn' and self._key_attr not in _attrlist:
            _attrlist.append(self._key_attr)
        _filter = LDAPFilter(self._search_filter)
        _filter &= LDAPDictFilter(self._search_criteria)
        _filter &= LDAPFilter(queryFilter)
        _filter &= LDAPDictFilter(criteria)
        # XXX: Is it really good to filter out entries without the key attr or
        # would it be better to fail? (see also __iter__ secondary key)
        if self._key_attr != 'rdn' and self._key_attr not in _filter:
            _filter &= '(%s=*)' % (self._key_attr,)
        children = self._session.search(_filter.__str__(),
                                        self._search_scope,
                                        baseDN=self.DN,
                                        force_reload=self._reload,
                                        attrlist=_attrlist)
        if exact_match and len(children) != 1:
            # XXX: Is ValueError appropriate?
            # XXX: Really also fail, if there are 0 matches?
            raise ValueError(u"Exact match asked but search not exact")
        res = []
        for dn, attrs in children:
            key = self._calculate_key(dn, attrs)
            if attrlist is not None:
                resattr = dict([(k,v) for k,v in attrs.iteritems()
                        if k in attrlist])
                if 'dn' in attrlist:
                    resattr['dn'] = dn
                res.append((key, resattr))
            else:
                res.append(key)
        if exact_match:
            return res[0]
        else:
            return res

    def __iter__(self):
        """This is where keys are retrieved from ldap
        """
        if self.__name__ is None:
            return
        if self._reload:
            self._keys = None
            self._seckeys = None
            self._child_dns.clear()
        if self._keys is None and self._action != ACTION_ADD:
            self._keys = odict()
            attrlist = ['dn']
            if self._seckey_attrs:
                self._seckeys = dict()
                attrlist.extend(self._seckey_attrs)
            for key, attrs in self.search(attrlist=attrlist):
                try:
                    self._keys[key]
                except KeyError:
                    self._keys[key] = None
                    self._child_dns[key] = attrs['dn']
                    for seckey_attr, seckey in \
                            self._calculate_seckeys(attrs).items():
                        try:
                            self._seckeys[seckey_attr]
                        except KeyError:
                            self._seckeys[seckey_attr] = {}
                        try:
                            self._seckeys[seckey_attr][seckey]
                        except KeyError:
                            self._seckeys[seckey_attr][seckey] = key
                        else:
                            raise KeyError(
                                u"Secondary key not unique: %s='%s'." % \
                                        (seckey_attr, seckey))
                else:
                    raise RuntimeError(u"Key not unique: %s='%s'." % \
                            (self._key_attr, key))
        if self._keys:
            for key in self._keys:
                yield key
    
    iterkeys = __iter__
    
    def iteritems(self):
        for key in self:
            yield key, self[key]

    def sort(self, cmp=None, key=None, reverse=False):
        # XXX: a sort working only on the keys could work without wakeup -->
        # sortonkeys()
        #  first wake up all entries 
        dummy = self.items()
        if not dummy:
            return
        # second sort them
        self._keys.sort(cmp=cmp, key=key, reverse=reverse)
    
    def __getitem__(self, key):
        """Here nodes are created for keys, iff they do not exist already
        """
        if isinstance(key, str):
            key = decode(key)
        if not key in self:
            raise KeyError(u"Entry not existent: %s" % key)
        if self._keys[key] is not None:
            return super(LDAPNode, self).__getitem__(key)
        val = self._ChildClass()
        val._session = self._session
        # We are suppressing notification, as val is not really added to us,
        # rather, it is activated.
        self._notify_suppress = True
        super(LDAPNode, self).__setitem__(key, val)
        self._notify_suppress = False
        self._keys[key] = val
        return val

    def get(self, key, default=None):
        """Otherwise odict/pyodicts __getitem__ is used...

        XXX: Maybe this could be higher up in the hierarchy
        """
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key, val):
        if isinstance(key, str):
            key = decode(key)
        if self._search_scope is not ONELEVEL:
            raise NotImplementedError(
                    u"Adding with scope != ONELEVEL not supported.")
        if self._key_attr != 'rdn':
            raise NotImplementedError(u"Adding with key != rdn not supported.")
        val._session = self._session
        if self._keys is None:
            self._keys = odict()
        try:
            # a value with key is already in the directory
            self._keys[key]
        except KeyError, e:
            # the value is not yet in the directory 
            val._action = ACTION_ADD
            val.changed = True
            self.changed = True 
        self._notify_suppress = True
        super(LDAPNode, self).__setitem__(key, val)
        self._notify_suppress = False
        self._keys[key] = val    
        self._child_dns[key] = ','.join((key, self.DN))
        if val._action == ACTION_ADD:
            objectEventNotify(self.events['added'](val, newParent=self, 
                                                   newName=key))
    
    def __delitem__(self, key):
        """Do not delete immediately. Just mark LDAPNode to be deleted and
        remove key from self._keys.
        """
        if isinstance(key, str):
            key = decode(key)
        val = self[key]
        val._action = ACTION_DELETE
        # this will also trigger the changed chain
        val.changed = True
        del self._keys[key]
        try:
            self._deleted.append(val)
        except AttributeError:
            self._deleted = list()
            self._deleted.append(val)
    
    def __call__(self):
        if self.changed and self._action is not None:
            if self._action == ACTION_ADD:
                self._ldap_add()
            elif self._action == ACTION_MODIFY:
                self._ldap_modify()
            elif self._action == ACTION_DELETE:
                self._ldap_delete()
            if hasattr(self, '_attributes'):
                self.attributes.changed = False
            self.changed = False
            self._action = None                    
        if self._keys is None:
            return
        for node in self._keys.values() + getattr(self, '_deleted', []):
            if node is not None and node.changed:
                node()
    
    def __repr__(self):
        # XXX: This is mainly used in doctest, I think
        # doctest fails if we output utf-8
        dn = self.DN.encode('ascii', 'replace')
        name = self.__name__.encode('ascii', 'replace')
        if self.__parent__ is None:
            return "<%s - %s>" % (dn, self.changed)
        return "<%s:%s - %s>" % (dn, name, self.changed)
    
    __str__ = __repr__
    
    @property
    def noderepr(self):
        return repr(self)
    
    def _ldap_add(self):
        """adds self to the ldap directory.
        """
        self._session.add(self.DN, self.attributes)
    
    def _ldap_modify(self):
        """modifies attributs of self on the ldap directory.
        """ 
        modlist = list()
        orgin = self.attributes_factory(self)
        for key in orgin:
            # MOD_DELETE
            if not key in self.attributes:
                moddef = (MOD_DELETE, key, None)
                modlist.append(moddef)
        for key in self.attributes:
            # MOD_ADD
            if key not in orgin:
                moddef = (MOD_ADD, key, self.attributes[key])
                modlist.append(moddef)
            # MOD_REPLACE
            elif self.attributes[key] != orgin[key]:
                moddef = (MOD_REPLACE, key, self.attributes[key])
                modlist.append(moddef)
        if modlist:
            self._session.modify(self.DN, modlist)
    
    def _ldap_delete(self):
        """delete self from the ldap-directory.
        """
        self.__parent__._keys[self.__name__] = None
        super(LifecycleNode, self.__parent__).__delitem__(self.__name__)
        # XXX: Shouldnt this raise a KeyError
        del self.__parent__._keys[self.__name__]
        self._session.delete(self.DN)
    
    def _get_changed(self):
        return self._changed

    def _set_changed(self, value):
        """Set/Unset the changed flag

        Set:
            - if self.attrs are changed (attrs set us)
            - if a child is changed / added / removed (child sets us)
        Unset:
            - if neither a child nor the own attrs are changed (attrs or child
              tries to unset us)
        Anyway:
            - tell our parent in case we changed state
        """
        # only get active, if new state differs from old state
        oldval = self._changed
        if value is oldval:
            return
        if value:
            # Setting is easy
            self._changed = True
        else:
            # Unsetting needs more checks
            try:
                if self._attributes.changed:
                    return
            except AttributeError:
                # No attributes loaded, yet - cannot be changed
                pass
            childs = getattr(self, '_deleted', [])
            if self._keys is not None:
                childs.extend(filter(lambda x: x is not None, self._keys.values()))
            for child in childs:
                if child.changed:
                    return
            self._changed = False
        # And propagate to parent
        if self._changed is not oldval and self.__parent__ is not None:
            self.__parent__.changed = self._changed
            
    changed = property(_get_changed, _set_changed) 
    
    @property
    def ldap_session(self):
        return self._session
