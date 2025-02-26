from dataclasses import (dataclass,
                         field)

from pyro.TimeElapsed import TimeElapsed


@dataclass
class CompileData:
    time: TimeElapsed = field(init=False, default_factory=TimeElapsed)
    scripts_count: int = field(init=False, default_factory=int)
    success_count: int = field(init=False, default_factory=int)
    command_count: int = field(init=False, default_factory=int)

    def __post_init__(self):
        self.time = TimeElapsed()

    @property
    def failed_count(self) -> int:
        return self.command_count - self.success_count

    def to_string(self):
        raw_time, avg_time = ('{0:.3f}s'.format(t)
                              for t in (self.time.value(), self.time.average(self.success_count)))

        return f'Compile time: ' \
               f'{raw_time} ({avg_time}/script) - ' \
               f'{self.success_count} succeeded, ' \
               f'{self.failed_count} failed ' \
               f'({self.scripts_count} scripts)'
