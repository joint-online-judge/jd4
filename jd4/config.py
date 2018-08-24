import argparse
from appdirs import user_config_dir
from asyncio import get_event_loop
from os import path
from ruamel import yaml

from jd4.log import logger

_CONFIG_DIR = user_config_dir('jd4')
_CONFIG_FILE = path.join(_CONFIG_DIR, 'config.yaml')


def _load_config():
    _parser = argparse.ArgumentParser()
    _parser.add_argument('--server-url', dest='server_url', default='http://127.0.0.1:34765/', type=str, help='cb4 url')
    _parser.add_argument('--uname', dest='uname', default='judge', type=str, help='cb4 judge username')
    _parser.add_argument('--password', dest='password', default='123456', type=str, help='cb4 judge password')
    _args = _parser.parse_args()
    _config = _args.__dict__
    try:
        with open(_CONFIG_FILE, encoding='utf-8') as file:
            _config.update(dict(yaml.load(file, Loader=yaml.RoundTripLoader)))
    except FileNotFoundError:
        logger.warn('Config file %s not found, using default command line options', _CONFIG_FILE)
    return _config


config = _load_config()
print(config)


async def save_config():
    def do_save_config():
        with open(_CONFIG_FILE, 'w', encoding='utf-8') as file:
            yaml.dump(config, file, Dumper=yaml.RoundTripDumper)

    await get_event_loop().run_in_executor(None, do_save_config)


if __name__ == '__main__':
    print(config)
