import json
import logging
import multiprocessing
import os
import sys
import time
from copy import deepcopy

import psutil

from pyro.Anonymizer import Anonymizer
from pyro.JsonLogger import JsonLogger
from pyro.PackageManager import PackageManager
from pyro.PapyrusProject import PapyrusProject
from pyro.PathHelper import PathHelper
from pyro.PexReader import PexReader
from pyro.ProcessManager import ProcessManager
from pyro.ProcessState import ProcessState
from pyro.TimeElapsed import TimeElapsed


class BuildFacade:
    log: logging.Logger = logging.getLogger('pyro')

    ppj: PapyrusProject = None
    log_file: JsonLogger = None

    time_elapsed: TimeElapsed = TimeElapsed()

    scripts_count: int = 0
    success_count: int = 0
    command_count: int = 0

    @property
    def failed_count(self) -> int:
        return self.command_count - self.success_count

    @property
    def build_time(self) -> str:
        raw_time, avg_time = ('{0:.3f}s'.format(t)
                              for t in (self.time_elapsed.value(), self.time_elapsed.average(self.success_count)))

        return f'Compilation time: ' \
               f'{raw_time} ({avg_time}/script) - ' \
               f'{self.success_count} succeeded, ' \
               f'{self.failed_count} failed ' \
               f'({self.scripts_count} scripts)'

    def __init__(self, ppj: PapyrusProject) -> None:
        self.ppj = ppj

        self.scripts_count = len(self.ppj.psc_paths)

        # WARN: if methods are renamed and their respective option names are not, this will break.
        options: dict = deepcopy(self.ppj.options.__dict__)

        for key in options:
            if key in ('args', 'input_path', 'anonymize', 'package', 'zip', 'zip_compression'):
                continue
            if key.startswith(('ignore_', 'no_', 'force_', 'resolve_')):
                continue
            if key.endswith('_token'):
                continue
            setattr(self.ppj.options, key, getattr(self.ppj, f'get_{key}')())

        # record project options in log
        if self.ppj.options.log_path:
            self._rotate_logs(5)

            os.makedirs(self.ppj.options.log_path, exist_ok=True)
            log_path = os.path.join(self.ppj.options.log_path, f'pyro-{int(time.time())}.log')
            with open(log_path, mode='w', encoding='utf-8') as f:
                json.dump(self.ppj.options.__dict__, f, indent=2)

        self.log_file = JsonLogger(ppj)
        self.log_file.add_record('project_data', {
            'program_path': ppj.program_path,
            'project_path': ppj.project_path,
            'import_paths': ppj.import_paths,
            'psc_paths': ppj.psc_paths,
            'pex_paths': ppj.pex_paths
        })

    def _rotate_logs(self, keep_count: int) -> None:
        if not os.path.isdir(self.ppj.options.log_path):
            return

        # because we're rotating at start, account for new log file
        keep_count -= 1

        log_files = [f for f in os.listdir(self.ppj.options.log_path) if f.endswith('.log')]
        if not len(log_files) > keep_count:
            return

        log_paths = [os.path.join(self.ppj.options.log_path, f) for f in log_files]

        logs_to_retain = log_paths[-keep_count:]
        logs_to_remove = [f for f in log_paths if f not in logs_to_retain]

        for f in logs_to_remove:
            try:
                os.remove(f)
            except PermissionError:
                BuildFacade.log.error(f'Cannot delete log file without permission: {f}')

    def _find_modified_scripts(self) -> list:
        pex_paths: list = []

        for psc_path in self.ppj.psc_paths:
            script_name, _ = os.path.splitext(os.path.basename(psc_path))

            # if pex exists, compare time_t in pex header with psc's last modified timestamp
            pex_match: list = [pex_path for pex_path in self.ppj.pex_paths if pex_path.endswith(f'{script_name}.pex')]
            if not pex_match:
                continue

            pex_path: str = pex_match[0]
            if not os.path.isfile(pex_path):
                continue

            try:
                header = PexReader.get_header(pex_path)
            except ValueError:
                BuildFacade.log.warning(f'Cannot determine compilation time due to unknown magic: "{pex_path}"')
                continue

            psc_last_modified: float = os.path.getmtime(psc_path)
            pex_last_compiled: float = float(header.compilation_time.value)

            # if psc is older than the pex
            if psc_last_modified < pex_last_compiled:
                pex_paths.append(pex_path)

        return PathHelper.uniqify(pex_paths)

    @staticmethod
    def _limit_priority() -> None:
        process = psutil.Process(os.getpid())
        process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == 'win32' else 19)

    def try_compile(self) -> None:
        """Builds and passes commands to Papyrus Compiler"""
        commands: list = self.ppj.build_commands()

        self.command_count = len(commands)

        self.time_elapsed.start_time = time.time()

        if self.ppj.options.no_parallel or self.command_count == 1:
            for command in commands:
                if ProcessManager.run_compiler(command) == ProcessState.SUCCESS:
                    self.success_count += 1
        elif self.command_count > 0:
            multiprocessing.freeze_support()
            worker_limit = min(self.command_count, self.ppj.options.worker_limit)
            pool = multiprocessing.Pool(processes=worker_limit,
                                        initializer=BuildFacade._limit_priority)
            for state in pool.imap(ProcessManager.run_compiler, commands):
                if state == ProcessState.SUCCESS:
                    self.success_count += 1
            pool.close()
            pool.join()

        self.time_elapsed.end_time = time.time()

    def try_anonymize(self) -> None:
        """Obfuscates identifying metadata in compiled scripts"""
        scripts: list = self._find_modified_scripts()

        if not scripts and not self.ppj.missing_scripts and not self.ppj.options.no_incremental_build:
            BuildFacade.log.error('Cannot anonymize compiled scripts because no source scripts were modified')
        else:
            # these are absolute paths. there's no reason to manipulate them.
            for pex_path in self.ppj.pex_paths:
                if not os.path.isfile(pex_path):
                    BuildFacade.log.warning(f'Cannot locate file to anonymize: "{pex_path}"')
                    continue

                Anonymizer.anonymize_script(pex_path)

    def try_pack(self) -> None:
        """Generates BSA/BA2 packages for project"""
        package_manager = PackageManager(self.ppj)
        package_manager.create_packages()

    def try_zip(self) -> None:
        """Generates ZIP file for project"""
        package_manager = PackageManager(self.ppj)
        package_manager.create_zip()
