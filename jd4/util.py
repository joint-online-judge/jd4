import re
from asyncio import get_event_loop, StreamReader, StreamReaderProtocol
from os import fdopen, listdir, open as os_open, path, remove, waitpid, walk, rmdir, chmod, \
    O_RDONLY, O_NONBLOCK, WEXITSTATUS, WIFSIGNALED, WNOHANG, WTERMSIG
from shutil import rmtree, copytree, copy2, move
import stat
import rarfile
import tarfile
import zipfile

from jd4.error import FormatError

# Code type constants
FILE_TYPE_TEXT = 0
FILE_TYPE_TAR = 1
FILE_TYPE_ZIP = 2
FILE_TYPE_RAR = 3

TIME_RE = re.compile(r'([0-9]+(?:\.[0-9]*)?)([mun]?)s?')
TIME_UNITS = {'': 1000000000, 'm': 1000000, 'u': 1000, 'n': 1}
MEMORY_RE = re.compile(r'([0-9]+(?:\.[0-9]*)?)([kmg]?)b?')
MEMORY_UNITS = {'': 1, 'k': 1024, 'm': 1048576, 'g': 1073741824}


def remove_under(*dirnames):
    for dirname in dirnames:
        for name in listdir(dirname):
            full_path = path.join(dirname, name)
            if path.isdir(full_path):
                rmtree(full_path)
            else:
                remove(full_path)


def wait_and_reap_zombies(pid):
    _, status = waitpid(pid, 0)
    try:
        while True:
            waitpid(-1, WNOHANG)
    except ChildProcessError:
        pass
    if WIFSIGNALED(status):
        return -WTERMSIG(status)
    return WEXITSTATUS(status)


def read_text_file(file, size=-1):
    with open(file) as f:
        return f.read(size)


def write_binary_file(file, data):
    with open(file, 'wb') as f:
        f.write(data)


def write_text_file(file, text):
    with open(file, 'w') as f:
        f.write(text)


async def read_pipe(file, size):
    loop = get_event_loop()
    reader = StreamReader()
    protocol = StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(
        lambda: protocol, fdopen(os_open(file, O_RDONLY | O_NONBLOCK)))
    chunks = list()
    while size > 0:
        chunk = await reader.read(size)
        if not chunk:
            break
        chunks.append(chunk)
        size -= len(chunk)
    transport.close()
    return b''.join(chunks)


def parse_time_ns(time_str):
    match = TIME_RE.fullmatch(time_str)
    if not match:
        raise FormatError(time_str, 'error parsing time')
    return int(float(match.group(1)) * TIME_UNITS[match.group(2)])


def parse_memory_bytes(memory_str):
    match = MEMORY_RE.fullmatch(memory_str)
    if not match:
        raise FormatError(memory_str, 'error parsing memory')
    return int(float(match.group(1)) * MEMORY_UNITS[match.group(2)])


def chmod_recursive(_dir, mode):
    for file in listdir(_dir):
        _path = path.join(_dir, file)
        if path.isfile(_path):
            chmod(_path, mode)
        elif path.isdir(_path):
            chmod_recursive(_path, mode)


def extract_tar_file(file_path, dest_dir):
    with tarfile.open(file_path) as file:
        
        import os
        
        def is_within_directory(directory, target):
            
            abs_directory = os.path.abspath(directory)
            abs_target = os.path.abspath(target)
        
            prefix = os.path.commonprefix([abs_directory, abs_target])
            
            return prefix == abs_directory
        
        def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
        
            for member in tar.getmembers():
                member_path = os.path.join(path, member.name)
                if not is_within_directory(path, member_path):
                    raise Exception("Attempted Path Traversal in Tar File")
        
            tar.extractall(path, members, numeric_owner=numeric_owner) 
            
        
        safe_extract(file, path=dest_dir)


def extract_zip_file(file_path, dest_dir):
    with zipfile.ZipFile(file_path) as file:
        file.extractall(path=dest_dir)


def extract_rar_file(file_path, dest_dir):
    with rarfile.RarFile(file_path) as file:
        file.extractall(path=dest_dir)


EXTRACT_OPEN_FUNC = {
    FILE_TYPE_TAR: extract_tar_file,
    FILE_TYPE_ZIP: extract_zip_file,
    FILE_TYPE_RAR: extract_rar_file
}


def extract_archive(tmp_dir, sandbox_dir, file_type):
    file_path = path.join(tmp_dir, 'code')
    open_func = EXTRACT_OPEN_FUNC.get(file_type)
    if open_func:
        open_func(file_path, sandbox_dir)
    chmod_recursive(sandbox_dir, stat.S_IROTH | stat.S_IRGRP | stat.S_IRUSR)
    remove(file_path)
    rmdir(tmp_dir)


def movetree(src, dst):
    # requires both src and dest to exist
    for item in listdir(src):
        s = path.join(src, item)
        d = path.join(dst, item)
        move(s, d)
    rmdir(src)
