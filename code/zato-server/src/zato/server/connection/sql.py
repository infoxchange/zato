# -*- coding: utf-8 -*-

"""
Copyright (C) 2010 Dariusz Suchojad <dsuch at zato.io>

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

""" Manages the server's SQL connection pools.
"""

# stdlib
from copy import deepcopy
from cStringIO import StringIO
from logging import DEBUG, getLogger
from threading import RLock
from traceback import format_exc
from time import time

# SQLAlchemy
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, scoped_session

# validate
from validate import is_boolean, is_integer, VdtTypeError

# Spring Python
from springpython.context import DisposableObject

# Zato
from zato.common import Inactive, PASSWORD_SHADOW
from zato.common.odb import engine_def, ping_queries
from zato.common.util import get_component_name

class SessionWrapper(object):
    """ Wraps an SQLAlchemy session.
    """
    def __init__(self):
        self.session_initialized = False
        self.pool = None
        self.config = None
        self.logger = getLogger(self.__class__.__name__)
        
    def init_session(self, name, config, pool, use_scoped_session=True, warn_on_ping_fail=False):
        self.config = config
        self.pool = pool
        
        try:
            self.pool.ping()
        except Exception, e:
            msg = 'Could not ping:[{}], session will be left uninitialized, e:[{}]'.format(name, format_exc(e))
            self.logger.warn(msg)
        else:
            if use_scoped_session:
                self._Session = scoped_session(sessionmaker(bind=self.pool.engine))
            else:
                self._Session = sessionmaker(bind=self.pool.engine)
                
            self._session = self._Session()
            self.session_initialized = True
    
    def session(self):
        return self._Session()
    
    def close(self):
        self._session.close()
    
class SQLConnectionPool(object):
    """ A pool of SQL connections wrapping an SQLAlchemy engine.
    """
    def __init__(self, name, config, config_no_sensitive):
        self.name = name
        self.config = config
        self.logger = getLogger(self.__class__.__name__)
        
        # Safe for printing out to logs, any sensitive data has been shadowed
        self.config_no_sensitive = config_no_sensitive 
        
        _extra = {}
        _extra['connect_args'] = {'application_name': get_component_name()}

        extra = self.config.get('extra') # Will be None at times
        if extra:
            extra = ';'.join(extra.splitlines())
            for line in extra.split(';'):
                original_line = line
                if line:
                    line = line.split('=')
                    if not len(line) == 2:
                        raise ValueError('Each line must be a single key=value entry, not [{}]'.format(original_line))
                    
                    key, value = line
                    value = value.strip()
                    
                    try:
                        value = is_boolean(value)
                    except VdtTypeError:
                        # It's cool, not a boolean
                        pass 
                    
                    try:
                        value = is_integer(value)
                    except VdtTypeError:
                        # OK, not an integer
                        pass 
                    
                    _extra[key.strip()] = value
        
        engine_url = engine_def.format(**config)
        self.engine = create_engine(engine_url, pool_size=int(config['pool_size']), **_extra)
        
        event.listen(self.engine, 'checkin', self.on_checkin)
        event.listen(self.engine, 'checkout', self.on_checkout)
        event.listen(self.engine, 'connect', self.on_connect)
        event.listen(self.engine, 'first_connect', self.on_first_connect)
        
    def __str__(self):
        return '<{} at {}, config:[{}]>'.format(self.__class__.__name__, hex(id(self)), self.config_no_sensitive)
    
    __repr__ = __str__
        
    def on_checkin(self, dbapi_conn, conn_record):
        if self.logger.isEnabledFor(DEBUG):
            msg = 'Checked in dbapi_conn:{}, conn_record:{}'.format(dbapi_conn, conn_record)
            self.logger.debug(msg)
            
    def on_checkout(self, dbapi_conn, conn_record, conn_proxy):
        if self.logger.isEnabledFor(DEBUG):
            msg = 'Checked out dbapi_conn:{}, conn_record:{}, conn_proxy:{}'.format(
                dbapi_conn, conn_record, conn_proxy)
            self.logger.debug(msg)
            
    def on_connect(self, dbapi_conn, conn_record):
        if self.logger.isEnabledFor(DEBUG):
            msg = 'Connect dbapi_conn:{}, conn_record:{}'.format(dbapi_conn, conn_record)
            self.logger.debug(msg)
            
    def on_first_connect(self, dbapi_conn, conn_record):
        if self.logger.isEnabledFor(DEBUG):
            msg = 'First connect dbapi_conn:{}, conn_record:{}'.format(dbapi_conn, conn_record)
            self.logger.debug(msg)
        
    def ping(self):
        """ Pings the SQL database and returns the response time, in milliseconds.
        """
        query = ping_queries[self.engine.name]

        self.logger.debug('About to ping the SQL connection pool:[{}], query:[{}]'.format(self.config_no_sensitive, query))

        start_time = time()
        self.engine.connect().execute(query)
        response_time = time() - start_time

        self.logger.debug('Ping OK, pool:[{0}], response_time:[{1:03.4f} s]'.format(self.config_no_sensitive, response_time))

        return response_time
    
    def _conn(self):
        """ Returns an SQLAlchemy connection object.
        """
        return self.engine.connect()
    
    conn = property(fget=_conn, doc=_conn.__doc__)
    
    def _impl(self):
        """ Returns the underlying connection's implementation, the SQLAlchemy engine.
        """
        return self.engine
    
    impl = property(fget=_impl, doc=_impl.__doc__)

class PoolStore(DisposableObject):
    """ A main class for accessing all of the SQL connection pools. Each server
    thread has its own store.
    """
    def __init__(self, sql_conn_class=SQLConnectionPool):
        super(PoolStore, self).__init__()
        self.sql_conn_class = sql_conn_class
        self._lock = RLock()
        self.wrappers = {}
        self.logger = getLogger(self.__class__.__name__)
        
    def __getitem__(self, name, enforce_is_active=True):
        """ Checks out the connection pool. If enforce_is_active is False,
        the pool's is_active flag will be ignored.
        """
        with self._lock:
            if enforce_is_active:
                wrapper = self.wrappers[name]
                if wrapper.config['is_active']:
                    return wrapper
                raise Inactive(name)
            else:
                return self.wrappers[name]
        
    get = __getitem__
        
    def __setitem__(self, name, config):
        """ Stops a connection pool if it exists and replaces it with a new one 
        using updated settings.
        """
        with self._lock:
            if name in self.wrappers:
                del self[name]
                
            config_no_sensitive = deepcopy(config)
            config_no_sensitive['password'] = PASSWORD_SHADOW
            pool = self.sql_conn_class(name, config, config_no_sensitive)

            wrapper = SessionWrapper()
            wrapper.init_session(name, config, pool)
            
            self.wrappers[name] = wrapper
    
    def __delitem__(self, name):
        """ Stops a pool and deletes it from the store.
        """
        with self._lock:
            self.wrappers[name].pool.engine.dispose()
            del self.wrappers[name]
            
    def __str__(self):
        out = StringIO()
        out.write('<{} at {} wrappers:['.format(self.__class__.__name__, hex(id(self))))
        out.write(', '.join(sorted(self.wrappers.keys())))
        out.write(']>')
        return out.getvalue()
    
    __repr__ = __str__
    
    def change_password(self, name, password):
        """ Updates the password which means recreating the pool using the new
        password.
        """
        with self._lock:
            self[name].pool.engine.dispose()
            config = deepcopy(self.wrappers[name].pool.config)
            config['password'] = password
            self[name] = config
        
    def destroy(self):
        """ Invoked when Spring Python's container is releasing the store.
        """
        with self._lock:
            for name, wrapper in self.wrappers.items():
                wrapper.pool.engine.dispose()
