from aiohttp import ClientError
from asyncio import gather, get_event_loop, sleep, shield, wait, FIRST_COMPLETED
from io import BytesIO
from os import path
from tempfile import mkdtemp

from jd4.api import VJ4Session
from jd4.case import read_config
from jd4.cache import cache_open, cache_invalidate
from jd4.cgroup import try_init_cgroup
from jd4.compile import build, has_lang
from jd4.config import config, save_config
from jd4.log import logger
from jd4.status import STATUS_ACCEPTED, STATUS_COMPILE_ERROR, \
    STATUS_SYSTEM_ERROR, STATUS_JUDGING, STATUS_COMPILING
from jd4.util import FILE_TYPE_TEXT

RETRY_DELAY_SEC = 3


class CompileError(Exception):
    pass


class JudgeHandler:
    def __init__(self, session, request, ws):
        self.session = session
        self.request = request
        self.ws = ws

    async def handle(self):
        logger.info('Request Received, start to handle')
        event = self.request.pop('event', None)
        if not event:
            await self.do_record()
        elif event == 'problem_data_change':
            await self.update_problem_data()
        else:
            logger.warning('Unknown event: %s', event)
        for key in self.request:
            logger.warning('Unused key in judge request: %s', key)
        logger.info('Request handled')

    async def do_record(self):
        self.tag = self.request.pop('tag')
        self.type = self.request.pop('type')
        self.domain_id = self.request.pop('domain_id')
        self.pid = self.request.pop('pid')
        self.rid = self.request.pop('rid')
        self.lang = self.request.pop('lang')
        self.code_type = self.request.pop('code_type')
        self.judge_category = self.request.pop('judge_category')
        self.judge_category = self.judge_category and self.judge_category.split(',') or []
        self.show_detail = self.request.pop('show_detail')

        logger.info('Record: domain_id %s, pid %s, rid %s', self.domain_id, self.pid, self.rid)

        try:
            if self.code_type == FILE_TYPE_TEXT:
                self.code = self.request.pop('code').encode()
            else:
                self.code = path.join(mkdtemp(prefix='jd4.code.'))
                self.request.pop('code')
                logger.info('Saving code file in %s', self.code)
                await self.session.record_code_data(self.rid, path.join(self.code, 'code'))

            # TODO(tc-imba) pretest not supported

            await self.prepare()
            if self.type == 0:
                await self.do_submission()
            elif self.type == 1:
                await self.do_pretest()
            else:
                raise Exception('Unsupported type: {}'.format(self.type))
        except CompileError:
            self.end(status=STATUS_COMPILE_ERROR, score=0, time_ms=0, memory_kb=0)
        except ClientError:
            raise
        except Exception as e:
            logger.exception(e)
            self.next(judge_text=repr(e))
            self.end(status=STATUS_SYSTEM_ERROR, score=0, time_ms=0, memory_kb=0)

    async def update_problem_data(self):
        domain_id = self.request.pop('domain_id')
        pid = str(self.request.pop('pid'))
        await cache_invalidate(domain_id, pid)
        logger.debug('Invalidated %s/%s', domain_id, pid)
        await update_problem_data(self.session)

    async def prepare(self):
        loop = get_event_loop()
        config_file = await loop.create_task(
            cache_open(self.session, self.domain_id, self.pid))
        if not has_lang(self.lang):
            raise SystemError('Unsupported language: {}'.format(self.lang))
        self.config = read_config(config_file, self.lang, self.judge_category)

    async def do_submission(self):
        # loop = get_event_loop()
        # cases_file_task = loop.create_task(cache_open(self.session, self.domain_id, self.pid))
        package = await self.build()
        # with await cases_file_task as cases_file:
        await self.judge(package)

    async def do_pretest(self):
        # loop = get_event_loop()
        logger.info('Pretest: %s, %s, %s', self.domain_id, self.pid, self.rid)
        # cases_data_task = loop.create_task(self.session.record_pretest_data(self.rid))
        package = await self.build()
        # with BytesIO(await cases_data_task) as cases_file:
        await self.judge(package)

    async def build(self):
        logger.info('Build started, language: %s', self.lang)
        self.next(status=STATUS_COMPILING)
        package, message, _, _ = await shield(
            build(self.lang, self.code, self.code_type, self.config))
        self.next(compiler_text=message)
        if not package:
            logger.debug('Compile error: %s', message)
            raise CompileError(message)
        logger.info('Build successfully')
        return package

    async def judge(self, package):
        cases = list(self.config['cases'])
        logger.info('Judge started, cases count: %d', len(cases))
        loop = get_event_loop()
        self.next(status=STATUS_JUDGING, progress=0)
        total_status = STATUS_ACCEPTED
        total_score = 0
        total_time_usage_ns = 0
        total_memory_usage_bytes = 0
        judge_tasks = list()
        for case in cases:
            judge_tasks.append(loop.create_task(case.judge(package)))
        for index, judge_task in enumerate(judge_tasks):
            # logger.info('Judge case %d start', index)
            status, score, time_usage_ns, memory_usage_bytes, stdout, stderr, answer, execute_status = await shield(judge_task)
            # if self.type == 1:
            #     judge_text = stderr.decode(encoding='utf-8', errors='replace')
            # else:
            #     judge_text = ''
            stderr = stderr.decode(encoding='utf-8', errors='replace')
            if self.show_detail:
                stdout = stdout.decode(encoding='utf-8', errors='replace')
                answer = answer.decode(encoding='utf-8', errors='replace')
            else:
                stdout = answer = ''
            self.next(status=STATUS_JUDGING,
                      case={'status': status,
                            'score': score,
                            'time_ms': time_usage_ns // 1000000,
                            'memory_kb': memory_usage_bytes // 1024,
                            'stdout': stdout,
                            'stderr': stderr,
                            'answer': answer,
                            'execute_status': execute_status},
                      progress=(index + 1) * 100 // len(cases))
            total_status = max(total_status, status)
            total_score += score
            total_time_usage_ns += time_usage_ns
            total_memory_usage_bytes = max(total_memory_usage_bytes, memory_usage_bytes)
            # logger.info('Judge case %d end', index)
        logger.info('Judge successfully')
        self.end(status=total_status,
                 score=total_score,
                 time_ms=total_time_usage_ns // 1000000,
                 memory_kb=total_memory_usage_bytes // 1024)

    def next(self, **kwargs):
        self.ws.send_json({'key': 'next', 'tag': self.tag, **kwargs})

    def end(self, **kwargs):
        self.ws.send_json({'key': 'end', 'tag': self.tag, **kwargs})


async def update_problem_data(session):
    logger.info('Update problem data')
    result = await session.judge_datalist(config.get('last_update_at', 0))
    for pid in result['pids']:
        await cache_invalidate(pid['domain_id'], str(pid['pid']))
        logger.debug('Invalidated %s/%s', pid['domain_id'], str(pid['pid']))
    config['last_update_at'] = result['time']
    await save_config()


async def do_judge(session):
    await update_problem_data(session)
    await session.judge_consume(JudgeHandler)


async def do_noop(session):
    while True:
        await sleep(3600)
        logger.info('Updating session')
        await session.judge_noop()


async def daemon():
    try_init_cgroup()

    async with VJ4Session(config['server_url']) as session:
        while True:
            try:
                await session.login_if_needed(config['uname'], config['password'])
                done, pending = await wait([do_judge(session), do_noop(session)],
                                           return_when=FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await gather(*done)
            except Exception as e:
                logger.exception(e)
            logger.info('Retrying after %d seconds', RETRY_DELAY_SEC)
            await sleep(RETRY_DELAY_SEC)


if __name__ == '__main__':
    get_event_loop().run_until_complete(daemon())
