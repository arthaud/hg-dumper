#!/usr/bin/env python2
from contextlib import closing
import argparse
import multiprocessing
import os
import os.path
import re
import socket
import subprocess
import sys
import urllib.parse as urlparse

import bs4
import mercurial.dispatch
import mercurial.util
import requests
import socks


def printf(fmt, *args, **kwargs):
    file = kwargs.pop('file', sys.stdout)

    if args:
        fmt = fmt % args

    file.write(fmt)
    file.flush()


def is_html(response):
    ''' Return True if the response is a HTML webpage '''
    return '<html>' in response.text


def get_indexed_files(response):
    ''' Return all the files in the directory index webpage '''
    html = bs4.BeautifulSoup(response.text, 'html.parser')
    files = []

    for link in html.find_all('a'):
        url = urlparse.urlparse(link.get('href'))

        if (url.path and
                url.path != '.' and
                url.path != '..' and
                not url.path.startswith('/') and
                not url.scheme and
                not url.netloc):
            files.append(url.path)

    return files


def create_intermediate_dirs(path):
    ''' Create intermediate directories, if necessary '''
    dirname, basename = os.path.split(path)

    if dirname and not os.path.exists(dirname):
        try:
            os.makedirs(dirname)
        except OSError:
            pass # race condition


class Worker(multiprocessing.Process):
    ''' Worker for process_tasks '''

    def __init__(self, pending_tasks, tasks_done, args):
        super(Worker, self).__init__()
        self.daemon = True
        self.pending_tasks = pending_tasks
        self.tasks_done = tasks_done
        self.args = args

    def run(self):
        # initialize process
        self.init(*self.args)

        # fetch and do tasks
        while True:
            task = self.pending_tasks.get(block=True)

            if task is None: # end signal
                return

            result = self.do_task(task, *self.args)

            assert isinstance(result, list), 'do_task() should return a list of tasks'

            self.tasks_done.put(result)

    def init(self, *args):
        raise NotImplementedError

    def do_task(self, task, *args):
        raise NotImplementedError


def process_tasks(initial_tasks, worker, jobs, args=(), tasks_done=None):
    ''' Process tasks in parallel '''

    if not initial_tasks:
        return

    tasks_seen = set(tasks_done) if tasks_done else set()
    pending_tasks = multiprocessing.Queue()
    tasks_done = multiprocessing.Queue()
    num_pending_tasks = 0

    # add all initial tasks in the queue
    for task in initial_tasks:
        assert task is not None

        if task not in tasks_seen:
            pending_tasks.put(task)
            num_pending_tasks += 1
            tasks_seen.add(task)

    # initialize processes
    processes = [worker(pending_tasks, tasks_done, args) for _ in range(jobs)]

    # launch them all
    for p in processes:
        p.start()

    # collect task results
    while num_pending_tasks > 0:
        task_result = tasks_done.get(block=True)
        num_pending_tasks -= 1

        for task in task_result:
            assert task is not None

            if task not in tasks_seen:
                pending_tasks.put(task)
                num_pending_tasks += 1
                tasks_seen.add(task)

    # send termination signal (task=None)
    for _ in range(jobs):
        pending_tasks.put(None)

    # join all
    for p in processes:
        p.join()


class DownloadWorker(Worker):
    ''' Download a list of files '''

    def init(self, url, directory, retry, timeout):
        self.session = requests.Session()
        self.session.mount(url, requests.adapters.HTTPAdapter(max_retries=retry))

    def do_task(self, filepath, url, directory, retry, timeout):
        with closing(self.session.get('%s/%s' % (url, filepath),
                                      allow_redirects=False,
                                      stream=True,
                                      timeout=timeout)) as response:
            printf('[-] Fetching %s/%s [%d]\n', url, filepath, response.status_code)

            if response.status_code != 200:
                return []

            abspath = os.path.abspath(os.path.join(directory, filepath))
            create_intermediate_dirs(abspath)

            # write file
            with open(abspath, 'wb') as f:
                for chunk in response.iter_content(4096):
                    f.write(chunk)

            return []


class RecursiveDownloadWorker(DownloadWorker):
    ''' Download a directory recursively '''

    def do_task(self, filepath, url, directory, retry, timeout):
        with closing(self.session.get('%s/%s' % (url, filepath),
                                      allow_redirects=False,
                                      stream=True,
                                      timeout=timeout)) as response:
            printf('[-] Fetching %s/%s [%d]\n', url, filepath, response.status_code)

            if (response.status_code in (301, 302) and
                    'Location' in response.headers and
                    response.headers['Location'].endswith(filepath + '/')):
                return [filepath + '/']

            if response.status_code != 200:
                return []

            if filepath.endswith('/'): # directory index
                assert is_html(response)

                return [filepath + filename for filename in get_indexed_files(response)]
            else: # file
                abspath = os.path.abspath(os.path.join(directory, filepath))
                create_intermediate_dirs(abspath)

                # write file
                with open(abspath, 'wb') as f:
                    for chunk in response.iter_content(4096):
                        f.write(chunk)

                return []


def fetch_hg(url, directory, jobs, retry, timeout):
    ''' Dump a mercurial repository into the output directory '''

    assert os.path.isdir(directory), '%s is not a directory' % directory
    assert not os.listdir(directory), '%s is not empty' % directory
    assert jobs >= 1, 'invalid number of jobs'
    assert retry >= 1, 'invalid number of retries'
    assert timeout >= 1, 'invalid timeout'

    # find base url
    url = url.rstrip('/')
    if url.endswith('requires'):
        url = url[:-8]
    url = url.rstrip('/')
    if url.endswith('.hg'):
        url = url[:-3]
    url = url.rstrip('/')

    # check for /.hg/requires
    printf('[-] Testing %s/.hg/requires', url)
    response = requests.get('%s/.hg/requires' % url, allow_redirects=False)
    printf('[%d]\n', response.status_code)

    if response.status_code != 200:
        printf('error: %s/.hg/requires does not exist\n', url, file=sys.stderr)
        return 1
    elif 'dotencode\n' not in response.text:
        printf('error: %s/.hg is not a .hg directory\n', url, file=sys.stderr)
        return 1

    # check for directory listing
    printf('[-] Testing %s/.hg/ ', url)
    response = requests.get('%s/.hg/' % url, allow_redirects=False)
    printf('[%d]\n', response.status_code)

    if response.status_code == 200 and is_html(response) and 'requires' in get_indexed_files(response):
        printf('[-] Fetching .hg recursively\n')
        process_tasks(['.hg/', '.hgignore'],
                      RecursiveDownloadWorker,
                      jobs,
                      args=(url, directory, retry, timeout))

        printf('[-] Running hg update -C\n')
        os.chdir(directory)
        subprocess.check_call(['hg', 'update', '-C'])
        return 0

    # no directory listing
    printf('[-] Fetching common files\n')
    tasks = [
        '.hg/00changelog.i',
        '.hg/branch',
        '.hg/cache/branch2-served',
        '.hg/cache/branchheads-served',
        '.hg/cache/checkisexec',
        '.hg/cache/checklink',
        '.hg/cache/checklink-target',
        '.hg/cache/checknoexec',
        '.hg/dirstate',
        '.hg/hgrc',
        '.hg/last-message.txt',
        '.hg/requires',
        '.hg/store',
        '.hg/store/00changelog.i',
        '.hg/store/00manifest.i',
        '.hg/store/fncache',
        '.hg/store/phaseroots',
        '.hg/store/undo',
        '.hg/store/undo.phaseroots',
        '.hg/undo.bookmarks',
        '.hg/undo.branch',
        '.hg/undo.desc',
        '.hg/undo.dirstate',
        '.hgignore',
    ]
    process_tasks(tasks,
                  DownloadWorker,
                  jobs,
                  args=(url, directory, retry, timeout))

    # run hg verify
    printf('[-] Running hg verify with hook on open()\n')

    os.chdir(directory)
    session = requests.Session()
    session.mount(url, requests.adapters.HTTPAdapter(max_retries=retry))
    hg_directory_path = os.path.join(directory, '.hg')

    def open_hook(fun):
        def wrapper(filename, *args, **kwargs):
            if filename.startswith(hg_directory_path) and not os.path.exists(filename):
                relpath = filename[len(hg_directory_path) + 1:]

                with closing(session.get('%s/.hg/%s' % (url, relpath),
                                         allow_redirects=False,
                                         stream=True,
                                         timeout=timeout)) as response:
                    printf('[-] Fetching %s/.hg/%s [%d]\n', url, relpath, response.status_code)

                    if response.status_code == 200:
                        create_intermediate_dirs(filename)

                        # write file
                        with open(filename, 'wb') as f:
                            for chunk in response.iter_content(4096):
                                f.write(chunk)

            return fun(filename, *args, **kwargs)
        return wrapper

    # add hook
    mercurial.util.posixfile = open_hook(mercurial.util.posixfile)

    # run hg verify
    mercurial.dispatch.dispatch(mercurial.dispatch.request(['verify']))

    printf('[-] Running hg update -C\n')

    # run hg update -C
    mercurial.dispatch.dispatch(mercurial.dispatch.request(['update', '-C']))

    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(usage='%(prog)s [options] URL DIR',
                                     description='Dump a mercurial repository from a website.')
    parser.add_argument('url', metavar='URL',
                        help='url')
    parser.add_argument('directory', metavar='DIR',
                        help='output directory')
    parser.add_argument('--proxy',
                        help='use the specified proxy')
    parser.add_argument('-j', '--jobs', type=int, default=10,
                        help='number of simultaneous requests')
    parser.add_argument('-r', '--retry', type=int, default=3,
                        help='number of request attempts before giving up')
    parser.add_argument('-t', '--timeout', type=int, default=3,
                        help='maximum time in seconds before giving up')
    args = parser.parse_args()

    # jobs
    if args.jobs < 1:
        parser.error('invalid number of jobs')

    # retry
    if args.retry < 1:
        parser.error('invalid number of retries')

    # timeout
    if args.timeout < 1:
        parser.error('invalid timeout')

    # proxy
    if args.proxy:
        proxy_valid = False

        for pattern, proxy_type in [
                (r'^socks5:(.*):(\d+)$', socks.PROXY_TYPE_SOCKS5),
                (r'^socks4:(.*):(\d+)$', socks.PROXY_TYPE_SOCKS4),
                (r'^http://(.*):(\d+)$', socks.PROXY_TYPE_HTTP),
                (r'^(.*):(\d+)$', socks.PROXY_TYPE_SOCKS5)]:
            m = re.match(pattern, args.proxy)
            if m:
                socks.setdefaultproxy(proxy_type, m.group(1), int(m.group(2)))
                socket.socket = socks.socksocket
                proxy_valid = True
                break

        if not proxy_valid:
            parser.error('invalid proxy')

    # output directory
    if not os.path.exists(args.directory):
        os.makedirs(args.directory)

    if not os.path.isdir(args.directory):
        parser.error('%s is not a directory' % args.directory)

    if os.listdir(args.directory):
        parser.error('%s is not empty' % args.directory)

    # fetch everything
    sys.exit(fetch_hg(args.url, args.directory, args.jobs, args.retry, args.timeout))
