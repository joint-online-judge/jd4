import csv
import re
import shlex

from asyncio import gather, get_event_loop
from functools import partial
from io import BytesIO, TextIOWrapper
from itertools import islice
from os import link, mkfifo, path
from ruamel import yaml
from socket import socket, AF_UNIX, SOCK_STREAM, SOCK_NONBLOCK
from threading import RLock
from zipfile import ZipFile, BadZipFile

from jd4._compare import compare_stream
from jd4.cgroup import wait_cgroup
from jd4.compile import build, has_lang
from jd4.error import FormatError
from jd4.pool import get_sandbox, put_sandbox
from jd4.status import STATUS_ACCEPTED, STATUS_WRONG_ANSWER, \
    STATUS_TIME_LIMIT_EXCEEDED, STATUS_MEMORY_LIMIT_EXCEEDED, \
    STATUS_RUNTIME_ERROR, STATUS_SYSTEM_ERROR
from jd4.util import read_pipe, parse_memory_bytes, parse_time_ns, movetree, read_text_file
from jd4.log import logger

CHUNK_SIZE = 32768
MAX_STDOUT_SIZE = 134217728
MAX_STDERR_SIZE = 8192
MAX_LOG_SIZE = 1024
DEFAULT_TIME_NS = 1000000000
DEFAULT_MEMORY_BYTES = 268435456
PROCESS_LIMIT = 64


class CaseBase:
    def __init__(self, time_limit_ns, memory_limit_bytes, process_limit, score,
                 execute_file=None, execute_args=None, index=0):
        self.time_limit_ns = time_limit_ns
        self.memory_limit_bytes = memory_limit_bytes
        self.process_limit = process_limit
        self.score = score
        self.execute_file = execute_file
        self.execute_args = execute_args
        self.index = index

    async def judge(self, package):
        loop = get_event_loop()
        sandbox, = await get_sandbox(1)
        logger.info('Judge case %d in %s', self.index, sandbox.sandbox_dir)
        try:
            executable = await package.install(sandbox, self.execute_file, self.execute_args)
            stdin_file = path.join(sandbox.in_dir, 'stdin')
            mkfifo(stdin_file)
            stdout_file = path.join(sandbox.in_dir, 'stdout')
            mkfifo(stdout_file)
            stderr_file = path.join(sandbox.in_dir, 'stderr')
            mkfifo(stderr_file)
            with socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK) as cgroup_sock:
                cgroup_sock.bind(path.join(sandbox.in_dir, 'cgroup'))
                cgroup_sock.listen()
                execute_task = loop.create_task(executable.execute(
                    sandbox,
                    stdin_file='/in/stdin',
                    stdout_file='/in/stdout',
                    stderr_file='/in/stderr',
                    cgroup_file='/in/cgroup'))
                others_task = gather(
                    loop.run_in_executor(None, self.do_input, stdin_file),
                    # loop.run_in_executor(None, self.do_output, stdout_file),
                    read_pipe(stdout_file, MAX_STDOUT_SIZE),
                    read_pipe(stderr_file, MAX_STDERR_SIZE),
                    wait_cgroup(cgroup_sock,
                                execute_task,
                                self.time_limit_ns,
                                self.time_limit_ns,
                                self.memory_limit_bytes,
                                self.process_limit))
                execute_status = await execute_task
                _, stdout, stderr, (time_usage_ns, memory_usage_bytes) = \
                    await others_task
                # @TODO @tc-imba not efficient
                correct = await loop.run_in_executor(None, self.do_output_str, stdout)
            if memory_usage_bytes >= self.memory_limit_bytes:
                status = STATUS_MEMORY_LIMIT_EXCEEDED
                score = 0
            elif time_usage_ns >= self.time_limit_ns:
                status = STATUS_TIME_LIMIT_EXCEEDED
                score = 0
            elif execute_status:
                status = STATUS_RUNTIME_ERROR
                score = 0
            elif not correct:
                status = STATUS_WRONG_ANSWER
                score = 0
            else:
                status = STATUS_ACCEPTED
                score = self.score
            # print(correct)
            # print(stderr)
            # logger.info('case %d stdout: %s', self.index, str(stdout[0:MAX_LOG_SIZE]))
            # logger.info('case %d stderr: %s', self.index, str(stderr[0:MAX_LOG_SIZE]))
        except Exception as e:
            logger.error('Judge case %d Error', self.index)
            logger.exception(e)
            status = STATUS_SYSTEM_ERROR
            score = 0
            time_usage_ns = 0
            memory_usage_bytes = 0
            stdout = bytes()
            stderr = e.__str__().encode(encoding='utf-8')
            execute_status = 0
        finally:
            put_sandbox(sandbox)
        stdout = stdout[0:MAX_LOG_SIZE]
        stderr = stderr[0:MAX_LOG_SIZE]
        answer = self.do_answer(MAX_LOG_SIZE)
        return status, score, time_usage_ns, memory_usage_bytes, stdout, stderr, answer, execute_status


def dos2unix(src, dst):
    while True:
        buf = src.read(CHUNK_SIZE)
        if not buf:
            break
        buf = buf.replace(b'\r', b'')
        dst.write(buf)


class DefaultCase(CaseBase):
    def __init__(self, open_input, open_output, time_ns, memory_bytes, score,
                 execute_file=None, execute_args=None, index=0):
        super().__init__(time_ns, memory_bytes, PROCESS_LIMIT, score, execute_file, execute_args, index)
        self.open_input = open_input
        self.open_output = open_output

    def do_input(self, input_file):
        try:
            with open(input_file, 'wb') as dst, self.open_input() as src:
                dos2unix(src, dst)
        except BrokenPipeError:
            pass

    def do_output(self, output_file):
        with open(output_file, 'rb') as out, self.open_output() as ans:
            return compare_stream(ans, out)

    def do_output_str(self, output_str):
        with BytesIO(output_str) as out, self.open_output() as ans:
            return compare_stream(ans, out)

    def do_answer(self, size):
        with self.open_output() as ans:
            return ans.read(size)


# not supported
class CustomJudgeCase:
    def __init__(self, open_input, time_ns, memory_bytes, open_judge, judge_lang):
        self.open_input = open_input
        self.time_ns = time_ns
        self.memory_bytes = memory_bytes
        self.open_judge = open_judge
        self.judge_lang = judge_lang

    async def judge(self, user_package):
        loop = get_event_loop()
        judge_package, message, _, _ = await build(
            self.judge_lang,
            await loop.run_in_executor(None, lambda: self.open_judge().read()))
        if not judge_package:
            return STATUS_SYSTEM_ERROR, 0, 0, 0, message
        user_sandbox, judge_sandbox = await get_sandbox(2)
        try:
            async def prepare_user_sandbox():
                await user_sandbox.reset()
                return await user_package.install(user_sandbox)

            async def prepare_judge_sandbox():
                await judge_sandbox.reset()
                return await judge_package.install(judge_sandbox)

            user_executable, judge_executable = \
                await gather(prepare_user_sandbox(), prepare_judge_sandbox())
            user_stdin_file = path.join(user_sandbox.in_dir, 'stdin')
            mkfifo(user_stdin_file)
            user_stdout_file = path.join(user_sandbox.in_dir, 'stdout')
            mkfifo(user_stdout_file)
            judge_stdin_file = path.join(judge_sandbox.in_dir, 'stdin')
            link(user_stdout_file, judge_stdin_file)
            user_stderr_file = path.join(user_sandbox.in_dir, 'stderr')
            mkfifo(user_stderr_file)
            judge_stdout_file = path.join(judge_sandbox.in_dir, 'stdout')
            mkfifo(judge_stdout_file)
            judge_stderr_file = path.join(judge_sandbox.in_dir, 'stderr')
            mkfifo(judge_stderr_file)
            judge_extra_file = path.join(judge_sandbox.in_dir, 'extra')
            mkfifo(judge_extra_file)
            with socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK) as user_cgroup_sock, \
                    socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK) as judge_cgroup_sock:
                user_cgroup_sock.bind(path.join(user_sandbox.in_dir, 'cgroup'))
                judge_cgroup_sock.bind(path.join(judge_sandbox.in_dir, 'cgroup'))
                user_cgroup_sock.listen()
                judge_cgroup_sock.listen()
                user_execute_task = loop.create_task(user_executable.execute(
                    user_sandbox,
                    stdin_file='/in/stdin',
                    stdout_file='/in/stdout',
                    stderr_file='/in/stderr',
                    cgroup_file='/in/cgroup'))
                judge_execute_task = loop.create_task(judge_executable.execute(
                    judge_sandbox,
                    stdin_file='/in/stdin',
                    stdout_file='/in/stdout',
                    stderr_file='/in/stderr',
                    extra_file='/in/extra',
                    cgroup_file='/in/cgroup'))
                others_task = gather(
                    loop.run_in_executor(None, self.do_input, user_stdin_file),
                    loop.run_in_executor(None, self.do_input, judge_extra_file),
                    read_pipe(user_stderr_file, MAX_STDERR_SIZE),
                    read_pipe(judge_stdout_file, MAX_STDERR_SIZE),
                    read_pipe(judge_stderr_file, MAX_STDERR_SIZE),
                    wait_cgroup(user_cgroup_sock,
                                user_execute_task,
                                self.time_ns,
                                self.time_ns,
                                self.memory_bytes,
                                PROCESS_LIMIT),
                    wait_cgroup(judge_cgroup_sock,
                                judge_execute_task,
                                DEFAULT_TIME_NS,
                                self.time_ns + DEFAULT_TIME_NS,
                                DEFAULT_MEMORY_BYTES,
                                PROCESS_LIMIT))
                user_execute_status, judge_execute_status = await gather(
                    user_execute_task, judge_execute_task)
                _, _, user_stderr, judge_stdout, judge_stderr, \
                (user_time_usage_ns, user_memory_usage_bytes), \
                (judge_time_usage_ns, judge_memory_usage_bytes) = \
                    await others_task
            if (judge_execute_status or
                    judge_memory_usage_bytes >= DEFAULT_MEMORY_BYTES or
                    judge_time_usage_ns >= DEFAULT_TIME_NS):
                status = STATUS_SYSTEM_ERROR
                score = 0
            elif user_memory_usage_bytes >= self.memory_bytes:
                status = STATUS_MEMORY_LIMIT_EXCEEDED
                score = 0
            elif user_time_usage_ns >= self.time_ns:
                status = STATUS_TIME_LIMIT_EXCEEDED
                score = 0
            elif user_execute_status:
                status = STATUS_RUNTIME_ERROR
                score = 0
            else:
                try:
                    status, score = map(int, judge_stdout.split())
                except SystemError:
                    status = STATUS_SYSTEM_ERROR
                    score = 0
            return status, score, user_time_usage_ns, user_memory_usage_bytes, user_stderr
        finally:
            put_sandbox(user_sandbox, judge_sandbox)

    def do_input(self, input_file):
        try:
            with self.open_input() as src, open(input_file, 'wb') as dst:
                dos2unix(src, dst)
        except BrokenPipeError:
            pass


class APlusBCase(CaseBase):
    def __init__(self, a, b, time_limit_ns, memory_limit_bytes, score):
        super().__init__(time_limit_ns, memory_limit_bytes, PROCESS_LIMIT, score)
        self.a = a
        self.b = b

    def do_input(self, input_file):
        try:
            with open(input_file, 'w') as file:
                file.write('{} {}\n'.format(self.a, self.b))
        except BrokenPipeError:
            pass

    def do_output(self, output_file):
        with open(output_file, 'rb') as file:
            return compare_stream(BytesIO(str(self.a + self.b).encode()), file)


# deprecated in cb4
def read_legacy_cases(config, open):
    num_cases = int(config.readline())
    for line in islice(csv.reader(config, delimiter='|'), num_cases):
        input, output, time_str, score_str = line[:4]
        try:
            memory_bytes = int(float(line[4]) * 1024)
        except (IndexError, ValueError):
            memory_bytes = DEFAULT_MEMORY_BYTES
        yield DefaultCase(partial(open, path.join('input', input)),
                          partial(open, path.join('output', output)),
                          int(float(time_str) * 1000000000),
                          memory_bytes,
                          int(score_str))


# deprecated in cb4
def read_yaml_cases_old(config, open):
    for case in yaml.safe_load(config)['cases']:
        if 'judge' not in case:
            yield DefaultCase(partial(open, case['input']),
                              partial(open, case['output']),
                              parse_time_ns(case['time']),
                              parse_memory_bytes(case['memory']),
                              int(case['score']))
        else:
            yield CustomJudgeCase(partial(open, case['input']),
                                  parse_time_ns(case['time']),
                                  parse_memory_bytes(case['memory']),
                                  partial(open, case['judge']),
                                  path.splitext(case['judge'])[1][1:])


# deprecated in cb4
def read_cases(file):
    zip_file = ZipFile(file)
    canonical_dict = dict((name.lower(), name)
                          for name in zip_file.namelist())

    def open(name):
        try:
            return zip_file.open(canonical_dict[name.lower()])
        except KeyError:
            raise FileNotFoundError(name) from None

    try:
        config = TextIOWrapper(open('config.ini'),
                               encoding='utf-8', errors='replace')
        return read_legacy_cases(config, open)
    except FileNotFoundError:
        pass
    try:
        config = open('config.yaml')
        return read_yaml_cases_old(config, open)
    except FileNotFoundError:
        pass
    raise FormatError('config file not found')


def read_yaml_cases(cases, judge_category, open):
    judge_category = judge_category or ['pretest']
    index = 0
    for case in cases:
        execute_args = case.get('execute_args')
        execute_file = case.get('execute_file', None)
        if execute_args:
            execute_args = shlex.split(str(execute_args))
        category = case.get('category') or 'pretest'
        if category not in judge_category:
            continue
        index += 1
        if 'judge' not in case:
            yield DefaultCase(partial(open, case['input']),
                              partial(open, case['output']),
                              parse_time_ns(case['time']),
                              parse_memory_bytes(case['memory']),
                              int(case['score']),
                              execute_file,
                              execute_args,
                              index)
        else:
            yield CustomJudgeCase(partial(open, case['input']),
                                  parse_time_ns(case['time']),
                                  parse_memory_bytes(case['memory']),
                                  partial(open, case['judge']),
                                  path.splitext(case['judge'])[1][1:])


def read_yaml_config(config, lang, judge_category, ops):
    open = ops["open"]
    extract = ops["extract"]
    data = yaml.safe_load(config)
    data['lang'] = None
    # if not has_lang(lang):
    #     logger.warning('Unsupported language: %s', lang)
    #     return data
    for _lang in data['languages']:
        _language = _lang.get('language')
        if not _language:
            logger.warning('Language not defined')
            continue
        if not lang == _language:
            continue
        if _lang.get('compiler_args'):
            _lang['compiler_args'] = shlex.split(_lang['compiler_args'])
        if _lang.get('execute_args'):
            _lang['execute_args'] = shlex.split(_lang['execute_args'])
        data['lang'] = _lang
        break
    data['cases'] = read_yaml_cases(data.get('cases'), judge_category, open)
    # support for injecting other files:
    if 'compile_time_files' in data:
        # logger.info("Need to inject compile time files at '%s'", data['compile_time_files'])
        data['compile_time_files'] = partial(extract, data['compile_time_files'])
    if 'runtime_files' in data:
        # logger.info("Need to inject runtime files at '%s'", data['runtime_files'])
        data['runtime_files'] = partial(extract, data['runtime_files'])
    return data


def read_config(file, lang, judge_category):
    zip_file = ZipFile(file)
    canonical_dict = dict((name.lower(), name)
                          for name in zip_file.namelist())

    def _open(name):
        try:
            return zip_file.open(canonical_dict[name.lower()])
        except KeyError:
            raise FileNotFoundError(name) from None

    def _extract(name, dest, subfolder=True):
        file_found = False
        for compressed_file in canonical_dict:
            if compressed_file.startswith(name.lower()):
                # logger.info("Extracting '%s'", canonical_dict[compressed_file])
                zip_file.extract(canonical_dict[compressed_file], path=dest)
                file_found = True
        if not file_found:
            raise FileNotFoundError(name) from None
        if not subfolder:
            movetree(path.join(dest, name), dest)

    ops = {
        "open": _open,
        "extract": _extract
    }

    try:
        config = _open('config.yaml')
        return read_yaml_config(config, lang, judge_category, ops)
    except FileNotFoundError:
        pass
    raise FormatError('config file not found')
