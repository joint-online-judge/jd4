from asyncio import get_event_loop
from os import chdir, dup2, execve, fork, mkdir, open as os_open, path, waitpid, \
               O_RDONLY, O_WRONLY, WIFSIGNALED, WTERMSIG, WEXITSTATUS
from pty import STDIN_FILENO, STDOUT_FILENO, STDERR_FILENO
from shutil import copytree, rmtree
from tempfile import mkdtemp

from jd4.cgroup import enter_cgroup
from jd4.sandbox import create_sandbox

SPAWN_ENV = {'PATH': '/usr/bin:/bin'}

def convert_status(status):
    if WIFSIGNALED(status):
        return -WTERMSIG(status)
    return WEXITSTATUS(status)

class Executable:
    def __init__(self, execute_file, execute_args):
        self.execute_file = execute_file
        self.execute_args = execute_args

    async def execute(self, sandbox, *,
                stdin_file=None, stdout_file=None, stderr_file=None, cgroup_file=None):
        return await sandbox.marshal(lambda: self.do_execute(
            stdin_file, stdout_file, stderr_file, cgroup_file))

    def do_execute(self, stdin_file, stdout_file, stderr_file, cgroup_file):
        chdir('/io/package')
        pid = fork()
        if not pid:
            if stdin_file:
                dup2(os_open(stdin_file, O_RDONLY), STDIN_FILENO)
            if stdout_file:
                dup2(os_open(stdout_file, O_WRONLY), STDOUT_FILENO)
            if stderr_file:
                dup2(os_open(stderr_file, O_WRONLY), STDERR_FILENO)
            if cgroup_file:
                enter_cgroup(cgroup_file)
            execve(self.execute_file, self.execute_args, SPAWN_ENV)
        _, status = waitpid(pid, 0)
        return convert_status(status)

class Package:
    def __init__(self, package_dir, execute_file, execute_args):
        self.package_dir = package_dir
        self.execute_file = execute_file
        self.execute_args = execute_args

    def __del__(self):
        rmtree(self.package_dir)

    async def install(self, sandbox):
        loop = get_event_loop()
        await sandbox.reset()
        await loop.run_in_executor(None,
                                   copytree,
                                   path.join(self.package_dir, 'package'),
                                   path.join(sandbox.io_dir, 'package'))
        return Executable(self.execute_file, self.execute_args)

class Compiler:
    def __init__(self, compiler_file, compiler_args, code_file, execute_file, execute_args):
        self.compiler_file = compiler_file
        self.compiler_args = compiler_args
        self.code_file = code_file
        self.execute_file = execute_file
        self.execute_args = execute_args

    async def build(self, sandbox, code):
        loop = get_event_loop()
        await sandbox.reset()
        status = await sandbox.marshal(lambda: self.do_build(code))
        if status:
            return status, None
        package_dir = mkdtemp(prefix='jd4.package.')
        await loop.run_in_executor(None,
                                   copytree,
                                   sandbox.io_dir,
                                   path.join(package_dir, 'package'))
        return 0, Package(package_dir, self.execute_file, self.execute_args)

    def do_build(self, code):
        chdir('/')
        with open(self.code_file, 'wb') as f:
            f.write(code)
        pid = fork()
        if not pid:
            # TODO(iceboy): Time/memory limit.
            # TODO(iceboy): Read compiler output.
            execve(self.compiler_file, self.compiler_args, SPAWN_ENV)
        _, status = waitpid(pid, 0)
        return convert_status(status)

class Interpreter:
    def __init__(self, code_file, execute_file, execute_args):
        self.code_file = code_file
        self.execute_file = execute_file
        self.execute_args = execute_args

    def build(self, code):
        package_dir = mkdtemp(prefix='jd4.package.')
        mkdir(path.join(package_dir, 'package'))
        with open(path.join(package_dir, 'package', self.code_file), 'wb') as f:
            f.write(code)
        return Package(package_dir, self.execute_file, self.execute_args)

if __name__ == '__main__':
    async def main():
        sandbox = await create_sandbox()
        gcc = Compiler('/usr/bin/gcc', ['gcc', '-std=c99', '-o', '/io/foo', 'foo.c'],
                       'foo.c', 'foo', ['foo'])
        javac = Compiler('/usr/bin/javac', ['javac', '-d', 'io', 'Program.java'],
                         'Program.java', '/usr/bin/java', ['java', 'Program'])
        python = Interpreter('foo.py', '/usr/bin/python', ['python', 'foo.py'])
        _, package = await gcc.build(sandbox, b"""#include <stdio.h>
int main(void) {
    printf("hello c\\n");
}""")
        for i in range(10):
            executable = await package.install(sandbox)
            await executable.execute(sandbox)
        _, package = await javac.build(sandbox, b"""class Program {
    public static void main(String[] args) {
        System.out.println("hello java");
    }
}""")
        for i in range(10):
            executable = await package.install(sandbox)
            await executable.execute(sandbox)
        package = python.build(b"print 'hello python'\n")
        for i in range(10):
            executable = await package.install(sandbox)
            await executable.execute(sandbox)

    get_event_loop().run_until_complete(main())