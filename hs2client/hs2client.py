import logging
import sys

from .genthrift.TCLIService import TCLIService
from .genthrift.TCLIService import constants
from .genthrift.TCLIService import ttypes
from .commons import DBAPICursor, ParamEscaper

from thrift.protocol import TBinaryProtocol
from thrift.transport import TSocket
from thrift.transport import TTransport
from future.utils import iteritems

DEFAULT_PORT = 10000

_logger = logging.getLogger(__name__)


def _check_status(response):
    """Raise an OperationalError if the status is not success"""
    _logger.debug(response)
    if response.status.statusCode != ttypes.TStatusCode.SUCCESS_STATUS:
        # TODO make better exception
        raise ValueError(response)


class HiveParamEscaper(ParamEscaper):
    def escape_string(self, item):
        # backslashes and single quotes need to be escaped
        # TODO verify against parser
        # Need to decode UTF-8 because of old sqlalchemy.
        # Newer SQLAlchemy checks dialect.supports_unicode_binds before encoding Unicode strings
        # as byte strings. The old version always encodes Unicode as byte strings, which breaks
        # string formatting here.
        if isinstance(item, bytes):
            item = item.decode('utf-8')
        return "'{}'".format(
            item
            .replace('\\', '\\\\')
            .replace("'", "\\'")
            .replace('\r', '\\r')
            .replace('\n', '\\n')
            .replace('\t', '\\t')
        )


_escaper = HiveParamEscaper()


class HS2Client(TCLIService.Client):
    __client = None
    __isOpened = False

    def __init__(self, host=None, port=None, username=None, database='default', auth=None,
                 configuration=None, kerberos_service_name=None, password=None,
                 thrift_transport=None):
        self.logger = logging.getLogger(__name__)

        configuration = configuration or {}

        if (password is not None) != (auth in ('LDAP', 'CUSTOM')):
            raise ValueError("Password should be set if and only if in LDAP or CUSTOM mode; "
                             "Remove password or use one of those modes")
        if (kerberos_service_name is not None) != (auth == 'KERBEROS'):
            raise ValueError("kerberos_service_name should be set if and only if in KERBEROS mode")
        if thrift_transport is not None:
            has_incompatible_arg = (
                    host is not None
                    or port is not None
                    or auth is not None
                    or kerberos_service_name is not None
                    or password is not None
            )
            if has_incompatible_arg:
                raise ValueError("thrift_transport cannot be used with "
                                 "host/port/auth/kerberos_service_name/password")

        if thrift_transport is not None:
            self._transport = thrift_transport
        else:
            port = port or 10000
            auth = auth or 'NONE'
            socket = TSocket.TSocket(host, port)
            if auth == 'NOSASL':
                # NOSASL corresponds to hive.server2.authentication=NOSASL in hive-site.xml
                self._transport = TTransport.TBufferedTransport(socket)
            elif auth in ('LDAP', 'KERBEROS', 'NONE', 'CUSTOM'):
                # Defer import so package dependency is optional
                import sasl
                import thrift_sasl

                if auth == 'KERBEROS':
                    # KERBEROS mode in hive.server2.authentication is GSSAPI in sasl library
                    sasl_auth = 'GSSAPI'
                else:
                    sasl_auth = 'PLAIN'
                    if password is None:
                        # Password doesn't matter in NONE mode, just needs to be nonempty.
                        password = 'x'

                def sasl_factory():
                    sasl_client = sasl.Client()
                    sasl_client.setAttr('host', host)
                    if sasl_auth == 'GSSAPI':
                        sasl_client.setAttr('service', kerberos_service_name)
                    elif sasl_auth == 'PLAIN':
                        sasl_client.setAttr('username', username)
                        sasl_client.setAttr('password', password)
                    else:
                        raise AssertionError
                    sasl_client.init()
                    return sasl_client

                self._transport = thrift_sasl.TSaslClientTransport(sasl_factory, sasl_auth, socket)
            else:
                # All HS2 config options:
                # https://cwiki.apache.org/confluence/display/Hive/Setting+Up+HiveServer2#SettingUpHiveServer2-Configuration
                # PAM currently left to end user via thrift_transport option.
                raise NotImplementedError(
                    "Only NONE, NOSASL, LDAP, KERBEROS, CUSTOM "
                    "authentication are supported, got {}".format(auth))

        protocol = TBinaryProtocol.TBinaryProtocol(self._transport)
        super(HS2Client, self).__init__(protocol)
        # oldest version that still contains features we care about
        # "V6 uses binary type for binary payload (was string) and uses columnar result set"
        protocol_version = ttypes.TProtocolVersion.HIVE_CLI_SERVICE_PROTOCOL_V6

        try:
            self._oprot.trans.open()
            self.__isOpened = True
            open_session_req = ttypes.TOpenSessionReq(
                client_protocol=protocol_version,
                configuration=configuration,
                username=username,
            )
            response = self.OpenSession(open_session_req)
            _check_status(response)
            assert response.sessionHandle is not None, "Expected a session from OpenSession"
            self._sessionHandle = response.sessionHandle
            assert response.serverProtocolVersion == protocol_version, \
                "Unable to handle protocol version {}".format(response.serverProtocolVersion)
            with self.cursor() as cursor:
                cursor.execute('USE `{}`'.format(database))
        except:
            self._oprot.trans.close()
            raise

    def open(self):
        # it has been opened in __init__
        return self

    def __enter__(self):
        return self.open()

    def close(self):
        self._oprot.trans.close()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def cursor(self, *args, **kwargs):
        """Return a new :py:class:`Cursor` object using the connection."""
        return Cursor(self, *args, **kwargs)


class Cursor(DBAPICursor):
    """These objects represent a database cursor, which is used to manage the context of a fetch
    operation.
    Cursors are not isolated, i.e., any changes done to the database by a cursor are immediately
    visible by other cursors or connections.
    """

    def __init__(self, connection, arraysize=1000):
        self._operationHandle = None
        super(Cursor, self).__init__()
        self.arraysize = arraysize
        self._connection = connection

    def _reset_state(self):
        """Reset state about the previous query in preparation for running another query"""
        super(Cursor, self)._reset_state()
        self._description = None
        if self._operationHandle is not None:
            request = ttypes.TCloseOperationReq(self._operationHandle)
            try:
                response = self._connection.CloseOperation(request)
                _check_status(response)
            finally:
                self._operationHandle = None

    @property
    def description(self):
        """This read-only attribute is a sequence of 7-item sequences.
        Each of these sequences contains information describing one result column:
        - name
        - type_code
        - display_size (None in current implementation)
        - internal_size (None in current implementation)
        - precision (None in current implementation)
        - scale (None in current implementation)
        - null_ok (always True in current implementation)
        This attribute will be ``None`` for operations that do not return rows or if the cursor has
        not had an operation invoked via the :py:meth:`execute` method yet.
        The ``type_code`` can be interpreted by comparing it to the Type Objects specified in the
        section below.
        """
        if self._operationHandle is None or not self._operationHandle.hasResultSet:
            return None
        if self._description is None:
            req = ttypes.TGetResultSetMetadataReq(self._operationHandle)
            response = self._connection.GetResultSetMetadata(req)
            _check_status(response)
            columns = response.schema.columns
            self._description = []
            for col in columns:
                primary_type_entry = col.typeDesc.types[0]
                if primary_type_entry.primitiveEntry is None:
                    # All fancy stuff maps to string
                    type_code = ttypes.TTypeId._VALUES_TO_NAMES[ttypes.TTypeId.STRING_TYPE]
                else:
                    type_id = primary_type_entry.primitiveEntry.type
                    type_code = ttypes.TTypeId._VALUES_TO_NAMES[type_id]
                self._description.append((
                    col.columnName.decode('utf-8') if sys.version_info[0] == 2 else col.columnName,
                    type_code.decode('utf-8') if sys.version_info[0] == 2 else type_code,
                    None, None, None, None, True
                ))
        return self._description

    def open(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        """Close the operation handle"""
        self._reset_state()

    def execute(self, operation, parameters=None, async=False):
        """Prepare and execute a database operation (query or command).
        Return values are not defined.
        """
        # Prepare statement
        if parameters is None:
            sql = operation
        else:
            sql = operation % _escaper.escape_args(parameters)

        self._reset_state()

        self._state = self._STATE_RUNNING
        _logger.info('%s', sql)

        req = ttypes.TExecuteStatementReq(self._connection._sessionHandle,
                                          sql, runAsync=async)
        _logger.debug(req)
        response = self._connection.ExecuteStatement(req)
        _check_status(response)
        self._operationHandle = response.operationHandle

    def cancel(self):
        req = ttypes.TCancelOperationReq(
            operationHandle=self._operationHandle,
        )
        response = self._connection.CancelOperation(req)
        _check_status(response)

    def _fetch_more(self):
        """Send another TFetchResultsReq and update state"""
        assert (self._state == self._STATE_RUNNING), "Should be running when in _fetch_more"
        assert (self._operationHandle is not None), "Should have an op handle in _fetch_more"
        if not self._operationHandle.hasResultSet:
            raise ValueError("No result set")
        req = ttypes.TFetchResultsReq(
            operationHandle=self._operationHandle,
            orientation=ttypes.TFetchOrientation.FETCH_NEXT,
            maxRows=self.arraysize,
        )
        response = self._connection.FetchResults(req)
        _check_status(response)
        assert not response.results.rows, 'expected data in columnar format'
        columns = map(_unwrap_column, response.results.columns)
        new_data = list(zip(*columns))
        self._data += new_data
        # response.hasMoreRows seems to always be False, so we instead check the number of rows
        # https://github.com/apache/hive/blob/release-1.2.1/service/src/java/org/apache/hive/service/cli/thrift/ThriftCLIService.java#L678
        # if not response.hasMoreRows:
        if not new_data:
            self._state = self._STATE_FINISHED

    def poll(self, get_progress_update=True):
        """Poll for and return the raw status data provided by the Hive Thrift REST API.
        :returns: ``ttypes.TGetOperationStatusResp``
        :raises: ``ProgrammingError`` when no query has been started
        .. note::
            This is not a part of DB-API.
        """
        if self._state == self._STATE_NONE:
            # TODO make ProgrammingError
            raise ValueError("No query yet")

        req = ttypes.TGetOperationStatusReq(
            operationHandle=self._operationHandle,
            getProgressUpdate=get_progress_update,
        )
        response = self._connection.GetOperationStatus(req)
        _check_status(response)

        return response

    def fetch_logs(self):
        """Retrieve the logs produced by the execution of the query.
        Can be called multiple times to fetch the logs produced after the previous call.
        :returns: list<str>
        :raises: ``ProgrammingError`` when no query has been started
        .. note::
            This is not a part of DB-API.
        """
        if self._state == self._STATE_NONE:
            # TODO make ProgrammingError
            raise ValueError("No query yet")

        try:  # Older Hive instances require logs to be retrieved using GetLog
            req = ttypes.TGetLogReq(operationHandle=self._operationHandle)
            logs = self._connection.GetLog(req).log.splitlines()
        except ttypes.TApplicationException as e:  # Otherwise, retrieve logs using newer method
            if e.type != ttypes.TApplicationException.UNKNOWN_METHOD:
                raise
            logs = []
            while True:
                req = ttypes.TFetchResultsReq(
                    operationHandle=self._operationHandle,
                    orientation=ttypes.TFetchOrientation.FETCH_NEXT,
                    maxRows=self.arraysize,
                    fetchType=1  # 0: results, 1: logs
                )
                response = self._connection.FetchResults(req)
                _check_status(response)
                assert not response.results.rows, 'expected data in columnar format'
                assert len(response.results.columns) == 1, response.results.columns
                new_logs = _unwrap_column(response.results.columns[0])
                logs += new_logs

                if not new_logs:
                    break

        return logs


def _unwrap_column(col):
    """Return a list of raw values from a TColumn instance."""
    for attr, wrapper in iteritems(col.__dict__):
        if wrapper is not None:
            result = wrapper.values
            nulls = wrapper.nulls  # bit set describing what's null
            assert isinstance(nulls, bytes)
            for i, char in enumerate(nulls):
                byte = ord(char) if sys.version_info[0] == 2 else char
                for b in range(8):
                    if byte & (1 << b):
                        result[i * 8 + b] = None
            return result
    raise ValueError("Got empty column value {}".format(col))  # pragma: no cover
