import shutil
from os import path
from typing import List, Tuple
from urllib.request import urlopen
import subprocess

import click

here = path.abspath(path.dirname(__file__))

PACKAGE = 'hs2client'
SUBPACKAGE = 'genthrift'
generated = 'gen-py'

HIVE_SERVER2_URL = 'https://raw.githubusercontent.com/apache/hive/branch-2.3/service-rpc/if/TCLIService.thrift'

config = {path.join("TCLIService", "TCLIService-remote"): [
    ('from TCLIService import TCLIService', 'from . import TCLIService'),
    ('from TCLIService.ttypes import *', 'from .ttypes import *')],
    path.join("TCLIService", "TCLIService.py"): [],
    path.join("TCLIService", "__init__.py"): [],
    path.join("TCLIService", "constants.py"): [],
    path.join("TCLIService", "ttypes.py"): [],
    '__init__.py': []}


def replace(file_path: str, replacements: List[Tuple[str, str]]) -> str:
    with open(file_path, 'r') as f:
        string = f.read()
    for old, new in replacements:
        string = string.replace(old, new)

    return string


def write_file(string: str, file_path: str) -> None:
    with open(file_path, 'w') as f:
        f.write(string)


def save_url(url):
    data = urlopen(url).read()
    file_path = path.join(here, url.rsplit('/', 1)[-1])
    with open(file_path, 'wb') as f:
        f.write(data)


@click.command()
@click.option('--hive_server2_url', default=HIVE_SERVER2_URL,
              help='The URL where the hive server2 thrift file can be downloaded')
@click.option('--package', default=PACKAGE, help='The package where the client should be placed')
@click.option('--subpackage', default=SUBPACKAGE, help='The subpackage where the client should be '
                                                    'placed')
def main(hive_server2_url, package, subpackage):
    save_url(hive_server2_url)
    hive_server2_path = path.join(here, hive_server2_url.rsplit('/', 1)[-1])

    subprocess.call(['thrift', '-r', '--gen', 'py', hive_server2_path])

    for file_path, replacement in config.items():
        to_write = replace(path.join(here, generated, file_path), replacement)
        write_file(to_write, path.join(here, package, subpackage, file_path))

    shutil.rmtree(generated)


if __name__ == '__main__':
    main()