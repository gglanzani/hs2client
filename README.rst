HS2Client
=========

This project aims to be an up to date Python client to interact with Hive Server 2
using the Thrift protocol.

Installation
------------

Install it with ``pip install hs2client`` or directly from source

.. code-block:: python

    python setup.py install

Usage
-----

Using it from Python is simple:

.. code-block:: python

    from hs2client.hs2client import HS2Client

    client = HS2Client(host='127.0.0.1', username='user', port=10000, auth='NONE')

    with client as connection, connection.cursor() as cursor:
        cursor.arraysize = 10
        cursor.execute('select * from table')
        for row in cursor:
            print(row)


Regenerate the Python thrift library
------------------------------------

The ``hs2client.py`` is just a thin wrapper around the generated Python code to
interact with Hive Server 2 through the Thrift protocol.

To regenerate the code using a newer version of the ``.thrift`` files, you can
use ``generate.py`` (note: you need to have ``thrift`` installed, see here_)

.. code-block:: sh

    python generate.py --help

    Usage: generate.py [OPTIONS]

    Options:
      --hive_server2_url TEXT The URL where the TCLIService.thrift file can be downloaded
      --package TEXT          The package where the client should be placed
      --subpackage TEXT       The subpackage where the client should be placed
      --help                  Show this message and exit.

Otherwise the defaults will be used.

.. _here: https://thrift-tutorial.readthedocs.io/en/latest/installation.html
